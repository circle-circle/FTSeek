"""
市场状态过滤模块

P0 修复 (2026-04-17):
- 数据源从 AlphaVantage 改为 Finnhub (VIX) + TwelveData (SPY)
- 避免 AlphaVantage 限流导致的 "Unknown error"
- 备用 LV3 推送数据 (通过 MarketDataRouter)

功能:
1. SPY 200 日均线过滤 - 判断市场趋势
2. VIX 恐慌指数过滤 - 判断市场情绪
3. 综合判断是否允许交易
"""
import logging
import os
from typing import Optional, Tuple
from datetime import datetime, timedelta
from dotenv import load_dotenv
from pathlib import Path

from app.utils.log_sanitize import scrub_secrets

logger = logging.getLogger(__name__)

# 从 .env.us 加载 API Keys
_env_path = Path(__file__).resolve().parent.parent.parent / ".env.us"
if _env_path.exists():
    load_dotenv(_env_path)

FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "")

# VIX 市场阶段分级
VIX_REGIMES = {
    "euphoria":  (0, 12),    # VIX < 12: 过度乐观，警惕回调
    "normal":    (12, 20),   # VIX 12-20: 正常
    "caution":   (20, 30),   # VIX 20-30: 谨慎
    "fear":      (30, 40),   # VIX 30-40: 恐慌
    "crisis":    (40, 100),  # VIX > 40: 危机
}

# VIX 交易门禁阈值 (市场情绪过滤)
# 现值 28: unified_config.yaml risk.vix_block_threshold=28, 代码回退同为 28 (2026-08-28 核对)。
#   原注释"默认 22"是 RFC-055 (2026-06-01, 25→22 数据驱动: HIGH_VOL regime SHORT 22.4%WR -$6,554)
#   的历史值, 早已不是任何一处的默认 — 保留此注释仅为溯源, 若按"恢复默认 22"理解会把门槛
#   收紧成 22 (VIX≥22 即拦 SHORT), 属风险偏好变更需人工裁决, 不可由注释纠偏顺带做。
def _load_vix_threshold() -> int:
    """从 unified_config.yaml 加载 VIX 门槛, 失败回退 28 (与 yaml 现值一致)."""
    try:
        from app.core.config_container import get_config
        cfg = get_config()
        if cfg:
            val = cfg.get_dotted("risk.vix_block_threshold", 28)
            return int(val)
    except Exception:
        pass
    return 28

VIX_THRESHOLD = _load_vix_threshold()


class MarketStateChecker:
    """市场状态检查器"""
    
    def __init__(self):
        self._spy_cache: Optional[Tuple[float, float, datetime]] = None  # (price, ma200, cached_at)
        self._vix_cache: Optional[Tuple[float, datetime]] = None  # (vix, cached_at)
        self._cache_ttl = timedelta(minutes=30)  # 缓存 30 分钟
        
        logger.info("市场状态检查器已初始化")
    
    def _fetch_spy_data(self) -> Optional[Tuple[float, float]]:
        """
        获取 SPY 当前价格和 200 日均线

        2026-04-20 升级: 直连 Futu LV3 快照 (唯一可用的数据源, key=*** 时 HTTP API 全挂)

        Returns:
            (当前价格，200 日均线) 或 None
        """
        # ★ 直连 Futu LV3 (不创建 Router, 避免重复连接冲突)
        try:
            from futu import OpenQuoteContext, KLType, AuType
            import os
            host = os.getenv("FUTU_OPEND_HOST", "127.0.0.1")
            port = int(os.getenv("FUTU_OPEND_PORT", "11111"))
            ctx = OpenQuoteContext(host=host, port=port)
            try:
                # 获取 SPY 实时快照
                ret, snap = ctx.get_market_snapshot(["US.SPY"])
                if ret != 0 or snap is None or len(snap) == 0:
                    logger.debug(f"LV3 SPY 快照失败: ret={ret}")
                    return None

                spy_price = float(snap.iloc[0].get("last_price", 0))
                if spy_price <= 0:
                    return None

                # 获取日线计算 MA200
                # 2026-06-30 根因修复: 加start/end + max_count>=范围交易日数
                from datetime import date as _d, timedelta as _td
                _end_spy = _d.today().isoformat()
                _start_spy = (_d.today() - _td(days=400)).isoformat()
                ret2, kline, _ = ctx.request_history_kline(
                    "US.SPY", ktype=KLType.K_DAY, max_count=320,
                    start=_start_spy, end=_end_spy
                )
                ma200 = None
                if ret2 == 0 and kline is not None and len(kline) >= 200:
                    ma200 = float(kline["close"].iloc[-200:].mean())
                    logger.info(f"SPY 当前价格={spy_price:.2f}, MA200={ma200:.2f} (LV3)")
                elif ret2 == 0 and len(kline) >= 50:
                    ma200 = float(kline["close"].mean())
                    logger.warning(f"SPY K 线不足 ({len(kline)} 条), 用均值估算 MA200={ma200:.2f}")
                else:
                    logger.debug(f"SPY 价格={spy_price:.2f}, MA200 K 线不足")

                return (spy_price, ma200)
            finally:
                ctx.close()
        except Exception as e:
            logger.debug(f"LV3 获取 SPY 失败: {e}")

        # HTTP API 全部不可用 (key=***), 静默跳过
        return None

    def get_vix_cached(self) -> Optional[float]:
        """带 30min 缓存的 VIX 读取, 取不到返回 None (fail-close 友好).

        与 vix_below_25() 的 fail-open 语义 (失败默认放行) 相反 —
        供 order_executor 下单前 VIX 新鲜度检查使用.
        2026-08-22 audit: 此前 order_executor 每单新建实例直调 _fetch_vix_data,
        绕过缓存 → 每单实时打 FRED (timeout=10). 抽出共用走单例缓存.

        Returns:
            VIX 值, 数据不可用时 None
        """
        now = datetime.now()
        if self._vix_cache:
            vix, cached_at = self._vix_cache
            if now - cached_at < self._cache_ttl:
                return vix
        vix = self._fetch_vix_data()
        if vix is not None:
            self._vix_cache = (vix, now)
        return vix

    def _fetch_vix_data(self) -> Optional[float]:
        """
        获取 VIX 恐慌指数

        2026-04-20 升级: FRED CSV 优先 (无需 API key) → AlphaVantage → 跳过 Finnhub

        2026-09-14 修复 (盘前数据有效性审计, 用户裁「全部处理」P0-1), 两处病灶:
          ① **兜底假值**: 原末行 `return 20.0  # 合理默认值` 使本函数**永不返回 None**,
             于是 get_vix_cached() 文档承诺的 "取不到返回 None (fail-close 友好)"
             是纸面的 — order_executor.py:3418 / playbook_service.py:932 两处
             `if vix is None: 拒绝新仓` 分支**不可达**, VIX 源全挂时反倒放行
             (与"回撤熔断静默失效"同型: 守卫恒不触发)。现改为如实返回 None,
             兜底由调用方按各自语义显式声明 (vix_below_25 → fail-open True,
             get_vix_regime → 显式 20.0, fail-close 侧 → 真拒绝)。
          ② **口径分裂**: 原链路 FRED 日频优先, 而 regime 侧
             (app/playbook/market_regime.py) 走 Futu VIXY×0.85 实时代理
             (2026-08-03 修复)。同一天的 VIX 门读两个口径 —— 实测 2026-09-14
             FRED VIXCLS 序列最新仅到 2026-09-10, 即执行硬拒门看的是 T-1/T-2
             收盘值, **今日盘中 VIX 冲高在门上不可见**, 与"新鲜度 fail-close"
             的注释意图相悖。现与 router 同条件补 VIXY 实时代理腿。
          ⚠️ 富途不供指数快照 (实测 US.VIX/US.IXIC/US.DJI/US.SPX 全 `_error: empty`,
             US.VIXY/US.SPY/US.QQQ 正常; 见 DEB-20260818-futu-capability-audit.md:89,144),
             故代理腿取 US.VIXY 而非直查 US.VIX。

        Returns:
            VIX 值; 所有源均不可用返回 None (**不再兜底 20.0**)。
        """
        import requests

        # ★ 0. 盘前/盘中: VIXY 实时代理优先 (与 MarketDataRouter 同口径同条件)
        #    VIX 现货无盘前交易 (CBOE 现金指数不撮合), 此处是盘前唯一有效值。
        #    盘中: 每 tick 变动, 捕捉当日 VIX 跳升 (FRED 日频做不到)。
        #    盘后/周末: 仍走 FRED 日频 (代理收盘后意义不大, 且与既有行为一致)。
        #    窗口判据 (哪些阶段算代理窗口) 收敛在单一真源:
        #    app/data/vix_source.py::is_vixy_proxy_window —— 2026-09-14 追加修复,
        #    修前这条规则在 4 个 VIX 生产者里各写一份且分成两派, 实测盘前会出现
        #    两个 VIX (执行门 14.71 vs 看板 vix_cache 17.84), 见 DEB-20260914 §八。
        try:
            from app.data.vix_source import fetch_vixy_via_opend, is_vixy_proxy_window
            if is_vixy_proxy_window():
                vixy_vix = fetch_vixy_via_opend()
                if vixy_vix is not None:
                    return vixy_vix
        except Exception as e:
            # 不许静默降级 (W4 debug-swallow 棘轮): VIXY 代理取不到意味着口径
            #   退化为 FRED 日频 (T-1/T-2) —— 这正是本次审计要暴露的那类退化。
            logger.warning("VIXY 实时代理获取 VIX 失败 (口径降级为 FRED 日频/T-1): %s",
                           e, exc_info=True)

        # ★ 优先: FRED CSV (免费公开接口, 无需 key) — 日频, 盘后/周末主源
        try:
            fred_url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=VIXCLS"
            response = requests.get(fred_url, timeout=10)
            if response.status_code == 200:
                lines = response.text.strip().split('\n')
                if len(lines) > 1:
                    last_line = lines[-1]
                    parts = last_line.split(',')
                    if len(parts) >= 2 and parts[1]:
                        vix = float(parts[1])
                        logger.info(f"VIX 当前值={vix:.2f} (FRED CSV)")
                        return vix
        except Exception as e:
            logger.debug(f"FRED CSV 获取 VIX 失败: {e}")

        # ★ 降级: AlphaVantage (仅当 key 有效时)
        av_key = os.getenv("ALPHAVANTAGE_API_KEY", "")
        if not av_key:
            av_key = os.getenv("FRED_API_KEY", "")

        if av_key and av_key != "***":
            try:
                url = "https://www.alphavantage.co/query"
                params = {
                    "function": "GLOBAL_QUOTE",
                    "symbol": "^VIX",
                    "apikey": av_key,
                }
                response = requests.get(url, params=params, timeout=10)
                response.raise_for_status()
                data = response.json()

                if "Global Quote" in data:
                    quote = data["Global Quote"]
                    price = float(quote.get("05. price", 0))
                    if price > 0:
                        logger.info(f"VIX 当前值={price:.2f} (AlphaVantage)")
                        return price
            except Exception as e:
                logger.debug(f"AlphaVantage 获取 VIX 失败: {scrub_secrets(e)}")

        # Finnhub 已跳过 (key=*** 无效, 免费版也返回 0)
        # 2026-09-14 (P0-1): 原此处 `return 20.0  # 合理默认值` — 兜底假值使
        #   fail-close 分支恒不可达 (详见函数 docstring ①)。现如实返回 None。
        logger.warning("VIX 数据源均不可用 (VIXY/FRED/AlphaVantage 全失败), 返回 None")
        return None
    
    def spy_above_ma200(self) -> bool:
        """
        检查 SPY 是否在 200 日均线上方
        
        Returns:
            True 如果 SPY > MA200，否则 False
        """
        # 检查缓存
        now = datetime.now()
        if self._spy_cache:
            price, ma200, cached_at = self._spy_cache
            if now - cached_at < self._cache_ttl:
                if ma200 is None:
                    logger.debug("使用缓存的 SPY 数据：价格已知但 MA200 不可用，默认放行")
                    return True
                result = price > ma200
                logger.debug(f"使用缓存的 SPY 数据：price={price:.2f}, MA200={ma200:.2f}, above={result}")
                return result
        
        # 获取新数据
        spy_data = self._fetch_spy_data()
        if not spy_data:
            logger.debug("无法获取 SPY 数据，默认允许交易（保守策略）")
            return True  # 数据获取失败时默认放行
        
        price, ma200 = spy_data
        self._spy_cache = (price, ma200, now)
        
        if ma200 is None:
            logger.debug(f"SPY 价格={price:.2f} 但 MA200 不可用，默认放行")
            return True  # MA200 不可用时默认放行
        
        result = price > ma200
        if result:
            logger.info(f"[市场趋势] SPY({price:.2f}) > MA200({ma200:.2f}) ✓ 上涨趋势")
        else:
            logger.warning(f"[市场趋势] SPY({price:.2f}) < MA200({ma200:.2f}) ✗ 下跌趋势，禁止交易")
        
        return result
    
    def vix_below_25(self) -> bool:
        """
        检查 VIX 是否低于 25（市场平稳）
        
        Returns:
            True 如果 VIX < 25，否则 False
        """
        # 检查缓存
        now = datetime.now()
        if self._vix_cache:
            vix, cached_at = self._vix_cache
            if now - cached_at < self._cache_ttl:
                result = vix < VIX_THRESHOLD
                logger.debug(f"使用缓存的 VIX 数据：vix={vix:.2f}, below_25={result}")
                return result
        
        # 获取新数据
        vix = self._fetch_vix_data()
        if vix is None:
            logger.warning("无法获取 VIX 数据，默认允许交易（保守策略）")
            return True  # 数据获取失败时默认放行
        
        self._vix_cache = (vix, now)
        
        result = vix < VIX_THRESHOLD
        if result:
            logger.info(f"[市场情绪] VIX({vix:.2f}) < 25 ✓ 市场平稳")
        else:
            logger.warning(f"[市场情绪] VIX({vix:.2f}) ≥ 25 ✗ 市场恐慌，禁止交易")
        
        return result
    
    def can_trade(self) -> bool:
        """
        综合判断是否允许交易
        
        条件:
        1. SPY > MA200（上涨趋势）
        2. VIX < 25（市场平稳）
        
        Returns:
            True 如果所有条件满足，否则 False
        """
        spy_ok = self.spy_above_ma200()
        vix_ok = self.vix_below_25()
        
        can_trade = spy_ok and vix_ok
        
        if can_trade:
            logger.info("[市场状态] 所有条件满足，允许交易 ✓")
        else:
            reasons = []
            if not spy_ok:
                reasons.append("SPY < MA200")
            if not vix_ok:
                reasons.append("VIX ≥ 25")
            logger.warning(f"[市场状态] 条件不满足，禁止交易 ✗ 原因：{', '.join(reasons)}")
        
        return can_trade
    
    def get_market_state_summary(self) -> dict:
        """
        获取市场状态摘要
        
        Returns:
            包含市场状态信息的字典
        """
        spy_ok = self.spy_above_ma200()
        vix_ok = self.vix_below_25()
        
        spy_data = self._spy_cache
        vix_data = self._vix_cache
        
        return {
            "can_trade": spy_ok and vix_ok,
            "spy_above_ma200": spy_ok,
            "spy_price": spy_data[0] if spy_data else None,
            "spy_ma200": spy_data[1] if spy_data else None,
            "vix_below_25": vix_ok,
            "vix_value": vix_data[0] if vix_data else None,
            "last_updated": datetime.now().isoformat(),
        }
    
    # === P2-1 新增: VIX 市场阶段感知 ===
    
    def get_vix_regime(self) -> str:
        """
        基于 VIX 判断市场阶段
        
        Returns:
            'euphoria' | 'normal' | 'caution' | 'fear' | 'crisis'
        """
        now = datetime.now()
        if self._vix_cache and (now - self._vix_cache[1]) < self._cache_ttl:
            vix = self._vix_cache[0]
        else:
            vix = self._fetch_vix_data()
            if vix is None:
                # 显式 fail-open 兜底 (2026-09-14 澄清; 只改注释与日志, 行为不变):
                #   20.0 落 VIX_REGIMES=(20,30) ⇒ "caution" ⇒ 仓位乘数 0.7。
                #   即"取不到数据 → 偏保守", 与本处的 fail-open 意图一致 (故意不改成
                #   fail-close, 见 tests/test_vix_single_source_contract.py)。
                #   注意 20.0 不是 "normal"(1.0) —— 原注释"默认正常"与边界不符, 已纠正。
                vix = 20.0
                logger.warning("VIX 取数失败 (全源不可用), regime 兜底 20.0 → caution (仓位×0.7)")
        
        for regime, (low, high) in VIX_REGIMES.items():
            if low <= vix < high:
                return regime
        return "crisis"
    
    def get_position_multiplier(self) -> float:
        """
        基于 VIX 市场阶段的仓位调整系数
        
        Returns:
            仓位乘数 (0.0-1.0)
        """
        regime = self.get_vix_regime()
        multipliers = {
            "euphoria": 0.7,   # 过度乐观，减仓防回调
            "normal": 1.0,     # 正常，满仓
            "caution": 0.7,    # 谨慎，降仓 30%
            "fear": 0.3,       # 恐慌，仅 30% 仓位
            "crisis": 0.0,     # 危机，清仓观望
        }
        mult = multipliers.get(regime, 0.5)
        logger.info(f"[VIX 阶段] {regime.upper()} → 仓位系数 {mult}")
        return mult
    
    def get_stop_loss_adjustment(self) -> float:
        """
        基于 VIX 的止损宽度调整系数
        
        Returns:
            止损宽度乘数 (>1 表示放宽止损)
        """
        regime = self.get_vix_regime()
        adjustments = {
            "euphoria": 0.8,   # 收紧止损
            "normal": 1.0,     # 标准
            "caution": 1.3,    # 放宽 30%
            "fear": 1.5,       # 放宽 50%
            "crisis": 2.0,     # 大幅放宽
        }
        return adjustments.get(regime, 1.0)


# 全局实例
_checker: Optional[MarketStateChecker] = None


def get_checker() -> MarketStateChecker:
    """获取全局市场状态检查器实例"""
    global _checker
    if _checker is None:
        _checker = MarketStateChecker()
    return _checker


def can_trade() -> bool:
    """
    检查当前是否允许交易
    
    Returns:
        True 如果允许交易，否则 False
    """
    return get_checker().can_trade()


def spy_above_ma200() -> bool:
    """检查 SPY 是否在 200 日均线上方"""
    return get_checker().spy_above_ma200()


def vix_below_25() -> bool:
    """检查 VIX 是否低于 25"""
    return get_checker().vix_below_25()


def get_market_state_summary() -> dict:
    """获取市场状态摘要"""
    return get_checker().get_market_state_summary()


if __name__ == "__main__":
    # 测试
    logging.basicConfig(level=logging.INFO)
    
    checker = MarketStateChecker()
    
    print("\n=== 市场状态检查 ===")
    print(f"SPY > MA200: {checker.spy_above_ma200()}")
    print(f"VIX < 25: {checker.vix_below_25()}")
    print(f"可以交易：{checker.can_trade()}")
    
    print("\n=== 市场状态摘要 ===")
    summary = checker.get_market_state_summary()
    for key, value in summary.items():
        print(f"{key}: {value}")
