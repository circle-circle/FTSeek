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
# RFC-055 (2026-06-01): 25→22, 数据驱动: HIGH_VOL regime SHORT 22.4%WR -$6,554
# 可配置: risk.vix_block_threshold in unified_config.yaml (默认 22)
def _load_vix_threshold() -> int:
    """从 unified_config.yaml 加载 VIX 门槛, 失败回退 22"""
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

    def _fetch_vix_data(self) -> Optional[float]:
        """
        获取 VIX 恐慌指数

        2026-04-20 升级: FRED CSV 优先 (无需 API key) → AlphaVantage → 跳过 Finnhub
        
        Returns:
            VIX 值或 None
        """
        import requests
        
        # ★ 优先: FRED CSV (免费公开接口, 无需 key)
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
                logger.debug(f"AlphaVantage 获取 VIX 失败: {e}")

        # Finnhub 已跳过 (key=*** 无效, 免费版也返回 0)
        logger.debug("VIX 数据源均不可用 (FRED/AlphaVantage 失败), 返回默认值 20.0")
        return 20.0  # 合理默认值
    
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
                vix = 20.0  # 默认正常
        
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
