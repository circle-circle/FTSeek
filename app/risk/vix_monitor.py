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

# 退避配置
BACKOFF_BASE = 60       # 初始退避间隔 (秒)
BACKOFF_MAX = 480       # 最大退避间隔 (秒)
BACKOFF_MULTIPLIER = 2  # 每次失败翻倍


class VIXMonitor:
    """
    VIX 指数监控器

    数据源优先级:
    1. FRED CSV API (免费、稳定、无频率限制)
    2. AlphaVantage API
    3. Yahoo Finance (按需)
    """

    def __init__(self, api_key: str = None):
        self.api_key = api_key or os.getenv("ALPHAVANTAGE_API_KEY", "")
        self._cache: Dict[str, Any] = self._load_cache()
        self._cache_lock = threading.Lock()  # P2 修复 (2026-05-11): 线程安全锁
        self._consecutive_failures = 0
        self._next_retry_at = 0.0
        self._last_success_time = 0.0

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
            logger.error("加载缓存失败：%s", e)
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
                return self._cache.get("vix")

        # 网络请求在锁外执行 (避免阻塞其他线程)
        # 多级 fallback: FRED → AlphaVantage → Finnhub(VIXY代理) → Yahoo
        vix = self._fetch_vix_fred()
        if vix is None:
            vix = self._fetch_vix_alphavantage()
        if vix is None:
            vix = self._fetch_vix_finnhub()
        if vix is None:
            vix = self._fetch_vix_fallback()

        if vix:
            self._mark_success()
            with self._cache_lock:
                self._cache = {
                    "vix": vix,
                    "timestamp": time.time(),
                    "source": "fred/alphavantage/finnhub/fallback",
                }
            self._save_cache(self._cache)
            logger.info("VIX 指数：%.2f", vix)
        else:
            self._mark_failure()

        return vix

    def _fetch_vix_fred(self) -> Optional[float]:
        """从 FRED CSV API 获取 VIX (主数据源, 免费无频率限制)"""
        try:
            url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=VIXCLS"
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            lines = resp.text.strip().split("\n")
            if len(lines) >= 2:
                parts = lines[-1].split(",")
                if len(parts) >= 2:
                    vix = float(parts[1])
                    logger.debug("VIX (FRED): %.2f", vix)
                    return vix
        except Exception as e:
            logger.debug("FRED VIX 获取失败: %s", e)
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
                    logger.debug("VIX (AlphaVantage): %.2f", price)
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
                vix_est = price * 0.85
                logger.debug("VIX (Finnhub/VIXY proxy): VIXY=$%.2f → VIX≈%.2f", price, vix_est)
                return round(vix_est, 2)
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
                    logger.debug("VIX (Yahoo): %.2f", price)
                    return price
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
            "vxv": None,
            "spread_pct": 0.0,
            "regime": "unknown",
            "adjustment": 1.0,
        }

        try:
            # 获取 VIX spot
            spot = self._fetch_vix_fred()
            if spot is None:
                spot = self._cache.get("vix")
            result["spot_vix"] = spot

            # 获取 VXV (3-month VIX futures)
            url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=VXVCLS"
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            lines = resp.text.strip().split("\n")
            if len(lines) >= 2:
                vxv = float(lines[-1].split(",")[1])
                result["vxv"] = vxv

                if spot and spot > 0:
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
        except Exception as e:
            result["status"] = "error"
            logger.debug("VIX 期限结构获取失败: %s", e)

        return result

    def check_vix_level(self) -> Dict:
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
        logger.error("推送 VIX 警报失败：%s", e)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    monitor = VIXMonitor()
    result = monitor.check_vix_level()
    print(f"VIX 检查结果：{result}")
