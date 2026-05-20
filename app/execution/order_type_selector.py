"""
订单类型选择器

根据信号评分和紧急度选择订单类型
"""
from typing import Optional
from app.core.models import SignalScore, OrderType


class OrderTypeSelector:
    """订单类型选择器"""
    
    def __init__(
        self,
        market_order_min_score: int = 80,
        limit_order_max_score: int = 60,
        twap_order_min_value: float = None,
    ):
        self.market_min_score = market_order_min_score
        self.limit_max_score = limit_order_max_score
        # P0 修复 (2026-05-01): None = 动态计算 (默认 equity 的 10%)
        self.twap_min_value = twap_order_min_value
    
    def _get_twap_threshold(self) -> float:
        """动态 TWAP 阈值：账户净值的 10%，最低 $5K"""
        if self.twap_min_value is not None and self.twap_min_value > 0:
            return self.twap_min_value
        # 动态计算
        try:
            from app.risk.capital_safety import get_capital_safety_system
            capital = get_capital_safety_system()
            equity = capital._state.current_equity if capital._state and capital._state.current_equity > 0 else 100000
            return max(equity * 0.10, 5000)
        except Exception:
            return 10000  # 安全 fallback
    
    def select(
        self,
        signal: SignalScore,
        urgency: str = "NORMAL",
        order_value: Optional[float] = None,
    ) -> OrderType:
        """选择订单类型"""
        # 大单使用 TWAP (动态阈值，适配不同资金量)
        twap_threshold = self._get_twap_threshold()
        if order_value and order_value > twap_threshold:
            return OrderType.TWAP
        
        # 高紧急度 + 高评分 → 市价单
        if urgency == "HIGH" and signal.strength >= self.market_min_score:
            return OrderType.MARKET
        
        # 高评分 → 市价单
        if signal.strength >= self.market_min_score:
            return OrderType.MARKET
        
        # 低评分 → 限价单
        if signal.strength <= self.limit_max_score:
            return OrderType.LIMIT
        
        # 中等评分 → IOC (立即成交或取消)
        return OrderType.IOC
