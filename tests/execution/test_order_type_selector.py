# tests/execution/test_order_type_selector.py
import pytest
from app.execution.order_type_selector import OrderTypeSelector
from app.core.models import SignalScore, Direction, OrderType

def test_select_market_order_for_high_strength():
    """测试高评分信号选择市价单"""
    selector = OrderTypeSelector()
    
    signal = SignalScore(
        symbol="AAPL",
        direction=Direction.LONG,
        strength=85,  # > 80
        confidence=0.9,
    )
    
    order_type = selector.select(signal, urgency="HIGH")
    assert order_type == OrderType.MARKET

def test_select_limit_order_for_low_strength():
    """测试低评分信号选择限价单"""
    selector = OrderTypeSelector()
    
    signal = SignalScore(
        symbol="AAPL",
        direction=Direction.LONG,
        strength=55,  # < 60
        confidence=0.6,
    )
    
    order_type = selector.select(signal)
    assert order_type == OrderType.LIMIT
