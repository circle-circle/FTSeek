"""
VIX 指数监控模块 - FTSeek-US

功能:
- 获取 VIX 恐慌指数实时数据 (FRED / AlphaVantage API)
- VIX 预警阈值从 voting_config.py 统一读取
- 缓存数据避免 API 超限
- 指数退避: API 连续失败后自动延长重试间隔
- 集成到风险引擎

P2 修复 (2026-04-17):
- VIX 阈值统一到 VotingConfig，消除多处硬编码
- 支持多档阈值：正常(<20)、警戒(20-25)、高波动(25-30)、极端(>30)

P3 修复 (2026-04-29):
- 指数退避: 连续失败后延长重试间隔 (60s -> 120s -> 240s -> 480s max)
- FRED 提升为主数据源 (免费、稳定、无频率限制)
- VIX 期限结构分析: contango/backwardation 识别 (VXV/VXST vs spot)
"""
import logging
import os
import time
import json
import threading
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
import requests

logger = logging.getLogger("ftseek_us.risk.vix_monitor")

# P2 修复: 从 VotingConfig 统一读取 VIX 阈值
try:
    from app.agents.voting.voting_config import VotingConfig
    _vix_config = VotingConfig.get_default_config()
    VIX_NORMAL_THRESHOLD = _vix_config.VIX_LOW_THRESHOLD    # 20
    VIX_WARNING_THRESHOLD = _vix_config.VIX_MID_THRESHOLD   # 25
    VIX_HIGH_THRESHOLD = _vix_config.VIX_HIGH_THRESHOLD     # 28 (Task #2: 统一)
    VIX_CRITICAL_THRESHOLD = 40.0
except Exception:
    VIX_NORMAL_THRESHOLD = 20.0
    VIX_WARNING_THRESHOLD = 25.0
    VIX_HIGH_THRESHOLD = 28.0  # Task #2: 统一为28 (was 30)
    VIX_CRITICAL_THRESHOLD = 40.0

# 缓存配置
from app.core.paths import get_vix_cache_dir
CACHE_DIR = get_vix_cache_dir()
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_FILE = CACHE_DIR / "vix_data.json"
CACHE_TTL_SECONDS = 180
CACHE_MAX_AGE_SECONDS = 3600  # P0 修复 (2026-05-11): 硬超时 1 小时，超时后返回 None 触发降级

# 跳变告警的"今日已告警"状态 (落盘, 重启不重复刷)
VIX_JUMP_STATE_PATH = Path(__file__).resolve().parents[2] / "data" / "vix_cache" / "vix_jump_state.json"

# 退避配置
BACKOFF_BASE = 60       # 初始退避间隔 (秒)
BACKOFF_MAX = 480       # 最大退避间隔 (秒)
BACKOFF_MULTIPLIER = 2  # 每次失败翻倍


class VIXMonitor:
    """
    VIX 指数监控器

    数据源优先级 (2026-09-14 更正; 原文只列 FRED/AV/Yahoo, 漏了 VIXY 且未写窗口):
      代理窗口 (盘前/交易时段, 判据 app/data/vix_source.py::is_vixy_proxy_window):
        1. Finnhub VIXY 实时代理 (VIXY×0.85)  2. FRED CSV  3. AlphaVantage  4. Yahoo
      其余时段 (盘后/隔夜/周末): 1. FRED CSV  2. AlphaVantage  3. Finnhub VIXY  4. Yahoo
      全失败 → None (fail-close, 消费方各自降级; 见 CACHE_MAX_AGE_SECONDS 注释)
    """

    def __init__(self, api_key: str = None):
        self.api_key = api_key or os.getenv("ALPHAVANTAGE_API_KEY", "")
        self._cache: Dict[str, Any] = self._load_cache()
        self._cache_lock = threading.Lock()  # P2 修复 (2026-05-11): 线程安全锁
        self._consecutive_failures = 0
        self._next_retry_at = 0.0
        self._last_success_time = 0.0
        # 观测日 (RFC-190 第三轮): 缓存里的 timestamp 是"写入时刻", 不是"数据观测日"。
        #   FRED VIXCLS 日频, 写入时刻可以是 T-2 收盘之后 ⇒ 读数面只有 timestamp
        #   时无法标注 as-of。这里记录**数据本身的日期**, 取不到则 None (不猜)。
        self._last_as_of = None

        logger.info("VIX 监控器已初始化 | 缓存 TTL=%ds | 退避策略 %d-%ds",
                     CACHE_TTL_SECONDS, BACKOFF_BASE, BACKOFF_MAX)

    def _backoff_seconds(self) -> int:
        return min(BACKOFF_BASE * (BACKOFF_MULTIPLIER ** self._consecutive_failures), BACKOFF_MAX)

    def _can_retry(self) -> bool:
        if self._next_retry_at == 0:
            return True
        if time.time() >= self._next_retry_at:
            return True
        return False

    def _mark_success(self):
        self._consecutive_failures = 0
        self._next_retry_at = 0.0
        self._last_success_time = time.time()

    def _mark_failure(self):
        self._consecutive_failures += 1
        delay = self._backoff_seconds()
        self._next_retry_at = time.time() + delay
        logger.warning("VIX 获取连续 %d 次失败，%ds 后重试",
                        self._consecutive_failures, delay)

    def _load_cache(self) -> Dict:
        try:
            if CACHE_FILE.exists():
                with open(CACHE_FILE, "r") as f:
                    data = json.load(f)
                    age = time.time() - data.get("timestamp", 0)
                    # P0 修复 (2026-05-11): 超过硬超时的缓存视为无效
                    if age > CACHE_MAX_AGE_SECONDS:
                        logger.warning(f"[VIX] 磁盘缓存已过期 ({age:.0f}s > {CACHE_MAX_AGE_SECONDS}s)，丢弃")
                        return {}
                    if age < CACHE_TTL_SECONDS:
                        logger.debug("使用缓存的 VIX 数据 (age=%.0fs)", age)
                        return data
                    else:
                        logger.debug("缓存已超出 TTL 但未超硬超时 (age=%.0fs)", age)
                        return data  # 仍可用但会尝试刷新
        except Exception as e:
            logger.error("加载缓存失败：%s", e, exc_info=True)
        return {}

    def _save_cache(self, data: Dict):
        try:
            with open(CACHE_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error("保存缓存失败：%s", e)

    def get_vix(self) -> Optional[float]:
        # P2 修复 (2026-05-11): 线程安全锁保护缓存读写
        with self._cache_lock:
            # P0 修复 (2026-05-11): 检查缓存硬超时
            cache_age = time.time() - self._cache.get("timestamp", 0)
            if cache_age > CACHE_MAX_AGE_SECONDS and self._cache.get("vix"):
                logger.warning(f"[VIX] 内存缓存硬超时 ({cache_age:.0f}s > {CACHE_MAX_AGE_SECONDS}s)，强制刷新")
                self._cache = {}  # 清除过期缓存

            # 极端行情时禁用缓存
            if self._cache.get("vix") and self._cache.get("vix", 0) > 25:
                logger.debug("VIX>25 极端行情，跳过缓存，重新获取")
            elif self._cache.get("vix") and time.time() - self._cache.get("timestamp", 0) < CACHE_TTL_SECONDS:
                self._last_as_of = self._cache.get("as_of")
                return self._cache["vix"]

            if not self._can_retry():
                remaining = self._next_retry_at - time.time()
                cache_age = time.time() - self._cache.get("timestamp", time.time())
                # P0 修复 (2026-05-11): 退避中也要检查缓存是否过期
                if cache_age > CACHE_MAX_AGE_SECONDS:
                    logger.error(f"[VIX] 退避中但缓存已过期 ({cache_age:.0f}s > {CACHE_MAX_AGE_SECONDS}s)，返回 None 触发降级")
                    return None
                logger.debug("退避中，使用缓存 (剩余 %.0fs, age=%.0fs)",
                              remaining, cache_age)
                self._last_as_of = self._cache.get("as_of")
                return self._cache.get("vix")

        # 网络请求在锁外执行 (避免阻塞其他线程)
        # P0 修复 (2026-06-21): 交易时段优先 Finnhub VIXY (实时), FRED 降为 fallback
        # 根因: FRED VIXCLS 是日频序列, 盘中永不变 → Rapid Regime Change 检测失效
        # 2026-09-14 (用户裁「修复再提交」): 窗口判据收敛到单一真源
        #   app/data/vix_source.py::is_vixy_proxy_window — 修前此处是
        #   `is_trading = phase == TRADING`, 即**盘前走 FRED-first**, 而执行门/regime
        #   走 VIXY-first ⇒ 同刻两个 VIX (本函数写的 data/vix_cache/vix_data.json
        #   实测 17.84 vs 执行门 14.71), 而本缓存供看板 data_service / day_score /
        #   order.thesis / data_health_check 读 ⇒ 人看到的值与系统决策用的值不是同一个。
        #   方向按 2026-08-03 事故证据统一 (盘前 FRED 滞后 2-3 天污染 regime)。
        from app.core.scheduler_triggers import get_market_phase
        from app.data.vix_source import is_vixy_proxy_window
        phase = get_market_phase()

        # 两种尝试顺序 (首个非 None 者胜) — 顺序由单一判据决定, 不再各写一份 if/else
        # 2026-09-24: (名称, 函数) 对 — source 字段从常数串改为记录**实际胜出源**,
        # 否则缓存里分不清 FRED 日频真值 vs VIXY×0.85 代理 (台账遗留 P1)。
        _vixy_first = (
            ("finnhub_vixy_proxy", self._fetch_vix_finnhub),  # VIXY 实时代理 (×0.85 单一真源)
            ("fred", self._fetch_vix_fred),                    # FRED 日频 fallback
            ("alphavantage", self._fetch_vix_alphavantage),
            ("yahoo_fallback", self._fetch_vix_fallback),      # Yahoo ^VIX (最后兜底, 失败返回 None)
        )
        _fred_first = (
            ("fred", self._fetch_vix_fred),           # FRED 日频 (T-1 收盘, 盘后/隔夜/周末此刻是真值)
            ("alphavantage", self._fetch_vix_alphavantage),
            ("finnhub_vixy_proxy", self._fetch_vix_finnhub),
            ("yahoo_fallback", self._fetch_vix_fallback),
        )
        _order = _vixy_first if is_vixy_proxy_window(phase) else _fred_first
        vix = None
        _src_name = None
        self._last_as_of = None  # 每次取数重置: 不得把上一次源的数据日期带给出价者
        for _name, _fetch in _order:
            vix = _fetch()
            if vix is not None:
                _src_name = _name
                break

        if vix:
            self._mark_success()
            with self._cache_lock:
                self._cache = {
                    "vix": vix,
                    "timestamp": time.time(),
                    "source": _src_name or "unknown",
                    "source_chain": "vixy_first" if _order is _vixy_first else "fred_first",
                    # 观测日 (RFC-190 第三轮): FRED → CSV 首列日期; 代理/实时源无
                    # 可核观测日 → None。读数面据此写 "as of 未知", 不拿写入时刻冒充。
                    "as_of": self._last_as_of,
                }
            self._save_cache(self._cache)
        else:
            self._mark_failure()

        return vix

    def _fetch_vix_fred(self) -> Optional[float]:
        """从 FRED CSV API 获取 VIX (主数据源, 免费无频率限制)

        2026-11-09 (RFC-190 第三轮): 同时记录**观测日** (CSV 首列), 并跳过
        FRED 用 '.' 占位的行 (假期/当日未发布)。修前 `lines[-1]` 直接 float(),
        遇到 '.' 会抛异常 → 整条 FRED 腿被吞成 None, 却没有任何口径信息。
        """
        try:
            url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=VIXCLS"
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            from app.data.fred_series import latest_valid_observation
            as_of, vix = latest_valid_observation(resp.text)
            if as_of is not None and vix is not None and vix > 0:
                self._last_as_of = as_of
                logger.debug("VIX (FRED): %.2f (as of %s)", vix, self._last_as_of)
                return vix
        except Exception as e:
            logger.warning("FRED VIX 获取失败: %s", e, exc_info=True)
        return None

    def _fetch_vix_alphavantage(self) -> Optional[float]:
        if not self.api_key:
            return None

        try:
            params = {
                "function": "GLOBAL_QUOTE",
                "symbol": "^VIX",
                "apikey": self.api_key,
            }
            resp = requests.get(
                "https://www.alphavantage.co/query",
                params=params, timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

            if "Global Quote" in data:
                quote = data["Global Quote"]
                price = float(quote.get("05. price", 0))
                if price > 0:
                    self._last_as_of = quote.get("07. latest trading day") or None
                    logger.debug("VIX (AlphaVantage): %.2f (as of %s)", price, self._last_as_of or "未知")
                    return price
        except Exception as e:
            logger.debug("AlphaVantage VIX 失败: %s", e)
        return None

    def _fetch_vix_finnhub(self) -> Optional[float]:
        """Finnhub VIXY 代理 (P1-1 修复: 中国服务器 FRED/CBOE 不可达时可用)
        
        原理: VIXY (iPath VIX Short-Term Futures ETF) 跟踪 VIX 短期期货,
        与 VIX spot 高度相关。转换公式: VIX ≈ VIXY * 0.85 (contango 校准因子)
        
        注意: 此转换在极端行情 (backwardation) 时偏差较大,
        但对 regime 分类 (LOW/NORMAL/HIGH) 足够准确。
        """
        try:
            api_key = os.environ.get("FINNHUB_API_KEY", "")
            if not api_key:
                return None
            url = f"https://finnhub.io/api/v1/quote?symbol=VIXY&token={api_key}"
            resp = requests.get(url, timeout=8)
            resp.raise_for_status()
            data = resp.json()
            price = data.get("c", 0)  # current price
            if price and price > 0:
                # VIXY → VIX 转换: 正常 contango 下 VIXY ≈ VIX * 1.15~1.20
                # 保守取 0.85 (略低估, 避免误触发 HIGH_VOL 拦截)
                # 2026-09-14 (P0-1): 换算系数收敛到单一真源 app/data/vix_source.py
                from app.data.vix_source import vixy_to_vix
                vix_est = vixy_to_vix(price)
                if vix_est is None:
                    return None
                try:
                    from app.utils.timezone import ET
                    timestamp = float(data["t"])
                    if timestamp <= 0:
                        raise ValueError("missing quote timestamp")
                    self._last_as_of = datetime.fromtimestamp(timestamp, tz=ET).date().isoformat()
                except (KeyError, TypeError, ValueError, OSError):
                    self._last_as_of = None
                logger.debug("VIX (Finnhub/VIXY proxy): VIXY=$%.2f → VIX≈%.2f (as of %s)",
                             price, vix_est, self._last_as_of or "未知")
                return vix_est
        except Exception as e:
            logger.debug("Finnhub VIXY 获取失败: %s", e)
        return None

    def _fetch_vix_fallback(self) -> Optional[float]:
        """Yahoo Finance 备用"""
        try:
            url = "https://query1.finance.yahoo.com/v8/finance/chart/^VIX"
            resp = requests.get(url, params={"range": "1d", "interval": "1m"}, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            if "chart" in data and "result" in data["chart"]:
                result = data["chart"]["result"][0]
                if "meta" in result and "regularMarketPrice" in result["meta"]:
                    price = result["meta"]["regularMarketPrice"]
                    if price and float(price) > 0:
                        try:
                            from app.utils.timezone import ET
                            timestamp = float(result["meta"]["regularMarketTime"])
                            if timestamp <= 0:
                                raise ValueError("missing quote timestamp")
                            self._last_as_of = datetime.fromtimestamp(timestamp, tz=ET).date().isoformat()
                        except (KeyError, TypeError, ValueError, OSError):
                            self._last_as_of = None
                        logger.debug("VIX (Yahoo): %.2f (as of %s)", price, self._last_as_of or "未知")
                        return float(price)
        except Exception as e:
            logger.debug("Yahoo Finance VIX 失败: %s", e)
        return None

    def get_term_structure(self) -> Dict[str, Any]:
        """
        P3 优化 2026-04-29: VIX 期限结构分析
        数据源: FRED VXVCLS (3-month VIX futures) vs VIXCLS (spot)

        contango (VXV > VIX): 市场预期远期波动 > 即期 → 定价未来风险, 当前平静
        backwardation (VXV < VIX): 即期恐慌 > 远期 → 市场急跌, 需要风控收紧

        Returns:
            {status, spot_vix, vxv, spread_pct, regime_adjustment}
        """
        result = {
            "status": "ok",
            "spot_vix": None,
            "spot_as_of": None,
            "vxv": None,
            "vxv_as_of": None,
            "spread_pct": None,
            "regime": "unknown",
            "adjustment": 1.0,
        }

        try:
            # 获取 VIX spot
            spot = self._fetch_vix_fred()
            spot_as_of = self._last_as_of
            if spot is None:
                spot = self._cache.get("vix")
                spot_as_of = self._cache.get("as_of")
            result["spot_vix"] = spot
            result["spot_as_of"] = spot_as_of

            # 获取 VXV (3-month VIX futures)
            url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=VXVCLS"
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            from app.data.fred_series import latest_valid_observation
            vxv_as_of, vxv = latest_valid_observation(resp.text)
            if vxv_as_of is not None and vxv is not None and vxv > 0:
                result["vxv"] = vxv
                result["vxv_as_of"] = vxv_as_of

                if spot and spot > 0 and spot_as_of == vxv_as_of:
                    spread = (vxv - spot) / spot
                    result["spread_pct"] = round(spread * 100, 2)

                    if spread > 0.05:  # contango > 5%
                        result["regime"] = "contango"
                        result["adjustment"] = 0.85  # 风控可适当放宽
                        logger.debug("VIX 期限结构: contango +%.1f%% (平静)", spread * 100)
                    elif spread < -0.03:  # backwardation < -3%
                        result["regime"] = "backwardation"
                        result["adjustment"] = 1.3  # 收紧风控
                        logger.warning("VIX 期限结构: backwardation %.1f%% (恐慌定价)", spread * 100)
                    else:
                        result["regime"] = "flat"
                        result["adjustment"] = 1.0
                else:
                    result["status"] = "mismatched_observation_date" if spot and spot > 0 else "unavailable"
            else:
                result["status"] = "unavailable"
        except Exception as e:
            result["status"] = "error"
            logger.debug("VIX 期限结构获取失败: %s", e)

        return result

    def _live_estimate(self) -> Dict:
        """T-1 真值锚 + VIXY 当日变化 ⇒ 当日水平估计 (只读, 不参与级别判定)。

        2026-11-09 (RFC-190 第六轮): 解决"水平准但慢、变化快但偏"的错配 ——
        级别 (level/message) 仍用真值, 本方法只额外给出"今天大概多少"。
        任何一环缺失 ⇒ est=None, 不猜测、不影响既有告警。
        """
        info = {"est": None, "prev_truth": None, "prev_as_of": None,
                "vixy_prev_close": None, "vixy_live": None, "change_pct": None}
        try:
            from app.data.vix_source import fetch_vixy_quote_via_opend, live_vixy_price
            from app.data.vix_estimator import estimate_level, change_pct, describe
            prev_truth = self._fetch_vix_fred()
            prev_as_of = getattr(self, "_last_as_of", None)
            quote = fetch_vixy_quote_via_opend()
            live = live_vixy_price(quote)
            prev_close = (quote or {}).get("prev_close")
            est = estimate_level(prev_truth, prev_close, live)
            info.update({
                "est": est, "prev_truth": prev_truth, "prev_as_of": prev_as_of,
                "vixy_prev_close": prev_close, "vixy_live": live,
                "change_pct": change_pct(prev_truth, est),
            })
            info["line"] = describe(prev_truth, prev_as_of, prev_close, live, est)
        except Exception as _e:
            logger.warning("[VIX] 实时估计失败 (不参与判定): %s", _e)
        return info

    def _jump_alert_due(self, prev_truth: Optional[float], est: Optional[float],
                        prev_as_of: Optional[str] = None) -> bool:
        """上跳告警: 每日最多一次 (状态落盘, 重启不重复刷)。

        锚点陈旧 (as_of 距今 > 5 个自然日, 如长假/FRED 断更) 时不告警: 那时
        "变化"可能跨了多个交易日, 用一天的变化率外推会系统性夸大。
        """
        from app.data.vix_estimator import is_jump
        if not is_jump(prev_truth, est):
            return False
        if prev_as_of:
            try:
                from datetime import date as _date
                from app.utils.timezone import now_et
                if (now_et().date() - _date.fromisoformat(prev_as_of)).days > 5:
                    logger.warning("[VIX] 跳变告警跳过: 锚点 as_of=%s 已过期", prev_as_of)
                    return False
            except Exception as exc:
                logger.warning("[VIX] 锚点新鲜度检查失败, 按未过期继续: %s", exc, exc_info=True)
        try:
            from app.utils.timezone import now_et
            today = now_et().date().isoformat()
            state_path = VIX_JUMP_STATE_PATH
            if state_path.exists():
                try:
                    if json.loads(state_path.read_text()).get("date") == today:
                        return False
                except Exception as exc:
                    logger.warning("[VIX] 跳变日标记读取失败(可能重复告警一天): %s", exc, exc_info=True)
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps({"date": today, "est": est}))
            return True
        except Exception as _e:
            logger.warning("[VIX] 跳变告警状态读写失败 (按未告警处理): %s", _e)
            return True

    def check_vix_level(self) -> Dict:
        """对外入口: 级别判定 (真值) + 实时估计 + 跳变告警标记。

        级别语义逐字不变; 新增字段对下游是纯加法:
          - `estimate`: {est, prev_truth, prev_as_of, vixy_prev_close, vixy_live, change_pct, line}
          - `jump`: 仅当日内上跳 >=25% 且今日未告警时出现。
        """
        res = self._check_vix_level_base()
        est = self._live_estimate()
        res["estimate"] = est
        # 供看板/接口读取 (只加字段, 不改 vix/source/as_of 语义)
        try:
            with self._cache_lock:
                if CACHE_FILE.exists():
                    _data = json.loads(CACHE_FILE.read_text())
                    _data["estimate"] = est
                    CACHE_FILE.write_text(json.dumps(_data, indent=2))
        except Exception as _ce:
            logger.debug("[VIX] 估计写入缓存失败 (不影响判定): %s", _ce)
        if self._jump_alert_due(est.get("prev_truth"), est.get("est"), est.get("prev_as_of")):
            res["jump"] = {
                "est": est.get("est"),
                "change_pct": est.get("change_pct"),
                "message": (f"⚡ VIX 跳变: T-1 真值 {est.get('prev_truth'):.2f} → 实时估计 "
                            f"{est.get('est'):.2f} ({est.get('change_pct'):+.0f}%, "
                            f"VIXY {(est.get('vixy_live') or 0):.2f} / prev "
                            f"{(est.get('vixy_prev_close') or 0):.2f})"),
                "recommendation": "波动率飙升: 复核在仓止损距离, 暂停追高开仓; 本值为估计非成交真值",
            }
        return res

    def _check_vix_level_base(self) -> Dict:
        """Check VIX level and return alert status.
        
        Returns dict with status, message, level, recommendation.
        """
        vix = self.get_vix()
        if vix is None:
            # P0 修复 (2026-05-11): 数据不可用时返回明确警告，而非 UNKNOWN
            return {
                "status": "warning",
                "message": "⚠️ VIX 数据源不可用，已启用保守风控模式",
                "vix": None,
                "level": "WARNING",
                "recommendation": "VIX 数据不可用超过 1 小时，建议降低仓位至 50%",
            }
        if vix >= VIX_CRITICAL_THRESHOLD:
            return {
                "status": "critical",
                "message": f"⚠️ VIX 指数 {vix:.2f} > {VIX_CRITICAL_THRESHOLD} (市场恐慌)",
                "vix": vix,
                "level": "CRITICAL",
                "recommendation": "建议减仓避险，暂停新开仓",
            }
        elif vix >= VIX_HIGH_THRESHOLD:
            return {
                "status": "high_volatility",
                "message": f"⚡ VIX 指数 {vix:.2f} >= {VIX_HIGH_THRESHOLD} (高波动)",
                "vix": vix,
                "level": "HIGH",
                "recommendation": "降低仓位至 50%，仅高确定性机会",
            }
        elif vix >= VIX_WARNING_THRESHOLD:
            return {
                "status": "warning",
                "message": f"🔶 VIX 指数 {vix:.2f} >= {VIX_WARNING_THRESHOLD} (警戒)",
                "vix": vix,
                "level": "WARNING",
                "recommendation": "谨慎交易，降低仓位",
            }
        else:
            return {
                "status": "normal",
                "message": f"✅ VIX 指数 {vix:.2f} (市场平稳)",
                "vix": vix,
                "level": "NORMAL",
                "recommendation": "正常交易",
            }

def send_vix_alert(feishu_webhook: str, vix_data: Dict):
    if not feishu_webhook:
        logger.warning("未配置飞书 Webhook")
        return

    level = vix_data.get("level", "UNKNOWN")
    message = vix_data.get("message", "")
    recommendation = vix_data.get("recommendation", "")

    if level == "NORMAL":
        return

    if level == "CRITICAL":
        title = "🚨 FTSeek 交易风险警报"
    else:
        title = "⚠️ FTSeek 交易风险提示"

    text = f"""{title}

**VIX 指数**: {vix_data.get('vix', 'N/A'):.2f}

**状态**: {message}

**建议**: {recommendation}

---
*FTSeek-US 风险监控系统*
*{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*
"""

    payload = {
        "msg_type": "text",
        "content": {
            "text": text,
        },
    }

    try:
        response = requests.post(
            feishu_webhook,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        response.raise_for_status()
        logger.info("VIX 警报已推送至飞书 | 级别=%s", level)
    except Exception as e:
        logger.error("推送 VIX 警报失败：%s", e, exc_info=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    monitor = VIXMonitor()
    result = monitor.check_vix_level()
    print(f"VIX 检查结果：{result}")
