"""
市场状态感知 Mixin

为策略提供统一的市场状态检测接口
优化于 2026-03-27: 阶段二优化 - 策略层市场状态感知
"""
import logging
from typing import Optional, Dict, Tuple
from collections import defaultdict

logger = logging.getLogger("ftseek.strategy.market_aware")


class MarketAwareMixin:
    """
    市场状态感知 Mixin
    
    使用方式:
        class MyStrategy(BaseStrategy, MarketAwareMixin):
            def on_kline(self, code, kline, kline_type):
                regime = self.get_market_regime(code)
                if regime and regime.regime_type == MarketRegimeType.RANGE_BOUND:
                    # 震荡市处理逻辑
                    pass
    """
    
    # 市场状态缓存
    _regime_cache: Dict[str, Tuple] = defaultdict(lambda: (None, 0))  # code -> (regime, timestamp)
    _cache_ttl = 300  # 缓存有效期 5 分钟
    
    def get_market_regime(self, code: str, use_cache: bool = True):
        """
        获取标的当前市场状态
        
        Args:
            code: 股票代码
            use_cache: 是否使用缓存
            
        Returns:
            MarketRegime 对象或 None
        """
        import time
        from .market_regime import market_regime_detector
        
        now = time.time()
        
        # 检查缓存
        if use_cache:
            cached = self._regime_cache.get(code)
            if cached and (now - cached[1]) < self._cache_ttl:
                return cached[0]
        
        try:
            regime = market_regime_detector.detect(code)
            if regime:
                self._regime_cache[code] = (regime, now)
            return regime
        except Exception as e:
            logger.debug(f"获取市场状态失败 {code}: {e}")
            return None
    
    def is_trending_market(self, code: str) -> bool:
        """判断是否为趋势市 (ADX > 25)"""
        regime = self.get_market_regime(code)
        if not regime:
            return False
        
        from .market_regime import MarketRegimeType
        return regime.regime_type in (
            MarketRegimeType.STRONG_TREND_UP,
            MarketRegimeType.WEAK_TREND_UP,
            MarketRegimeType.STRONG_TREND_DOWN,
            MarketRegimeType.WEAK_TREND_DOWN,
        )
    
    def is_range_bound(self, code: str) -> bool:
        """判断是否为震荡市 (ADX < 20)"""
        regime = self.get_market_regime(code)
        if not regime:
            return False
        
        from .market_regime import MarketRegimeType
        return regime.regime_type == MarketRegimeType.RANGE_BOUND
    
    def get_trend_direction(self, code: str) -> str:
        """
        获取趋势方向
        
        Returns:
            "up" | "down" | "neutral"
        """
        regime = self.get_market_regime(code)
        if not regime:
            return "neutral"
        
        from .market_regime import TrendDirection
        if regime.trend_direction in (TrendDirection.STRONG_UP, TrendDirection.WEAK_UP):
            return "up"
        elif regime.trend_direction in (TrendDirection.STRONG_DOWN, TrendDirection.WEAK_DOWN):
            return "down"
        return "neutral"
    
    def get_adx_value(self, code: str) -> Optional[float]:
        """获取 ADX 值"""
        regime = self.get_market_regime(code)
        return regime.adx_value if regime else None
    
    def should_avoid_signal(self, code: str, action: str) -> Tuple[bool, str]:
        """
        判断是否应该避免当前信号 (基于市场状态)
        
        Args:
            code: 股票代码
            action: "BUY" 或 "SELL"
            
        Returns:
            (should_avoid: bool, reason: str)
        """
        regime = self.get_market_regime(code)
        if not regime:
            return False, ""
        
        from .market_regime import MarketRegimeType, TrendDirection
        
        # 震荡市中，趋势策略信号降权
        if regime.regime_type == MarketRegimeType.RANGE_BOUND:
            if hasattr(self, 'name') and self.name in ('supertrend', 'macd'):
                return True, f"震荡市中趋势策略 {self.name} 信号降权"
        
        # 强趋势中，逆势信号降权
        if regime.trend_direction == TrendDirection.STRONG_UP and action == "SELL":
            return True, "强上涨趋势中避免做空"
        
        if regime.trend_direction == TrendDirection.STRONG_DOWN and action == "BUY":
            return True, "强下跌趋势中避免做多"
        
        return False, ""
    
    def get_adjusted_multiplier(self, code: str, base_multiplier: float) -> float:
        """
        根据市场状态调整 SuperTrend 乘数
        
        震荡市中增大乘数，降低灵敏度，减少假信号
        趋势市中保持原乘数
        """
        regime = self.get_market_regime(code)
        if not regime:
            return base_multiplier
        
        from .market_regime import MarketRegimeType
        
        # 震荡市中增大乘数
        if regime.regime_type == MarketRegimeType.RANGE_BOUND:
            adjusted = base_multiplier * 1.2  # 增大 20%
            logger.debug(f"[{code}] 震荡市 SuperTrend 乘数调整: {base_multiplier:.1f} -> {adjusted:.1f}")
            return adjusted
        
        return base_multiplier