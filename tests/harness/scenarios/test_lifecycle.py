"""
Lifecycle Tests
- Signal -> Order -> Fill -> Position -> Close
"""
import pytest
from app.core.models import Position, Direction, Order, OrderType
from tests.harness.mock_broker import MockFutuBroker

def test_order_to_position_lifecycle(broker: MockFutuBroker):
    """Test BUY -> FILL -> Position Created"""
    
    order = Order(
        symbol="US.AAPL",
        order_type=OrderType.MARKET,
        direction=Direction.LONG,
        quantity=10,
        price=150.0
    )
    
    result = broker.submit_order(order)
    
    assert result["status"] == "FILLED"
    assert "US.AAPL" in broker.positions
    assert broker.positions["US.AAPL"]["qty"] == 10.0
    assert broker.positions["US.AAPL"]["cost"] == 150.0

def test_position_close_lifecycle(broker: MockFutuBroker):
    """Test BUY -> SELL Close -> Position Removed"""
    
    # 1. Buy
    buy_order = Order(
        symbol="US.TSLA",
        order_type=OrderType.MARKET,
        direction=Direction.LONG,
        quantity=5,
        price=200.0
    )
    broker.submit_order(buy_order)
    assert broker.positions["US.TSLA"]["qty"] == 5.0
    
    # 2. Close (Sell)
    sell_order = Order(
        symbol="US.TSLA",
        order_type=OrderType.MARKET,
        direction=Direction.SHORT,
        quantity=5,
        price=210.0
    )
    broker.submit_order(sell_order)
    
    # 3. Verify empty
    assert "US.TSLA" not in broker.positions
    assert len(broker.orders) == 2

def test_partial_fill_logic(broker: MockFutuBroker):
    """Test Adding to Position (Pyramiding)"""
    
    # First order
    o1 = Order(symbol="US.QQQ", order_type=OrderType.MARKET, direction=Direction.LONG, quantity=10, price=400.0)
    broker.submit_order(o1)
    
    # Second order
    o2 = Order(symbol="US.QQQ", order_type=OrderType.MARKET, direction=Direction.LONG, quantity=10, price=402.0)
    broker.submit_order(o2)
    
    pos = broker.positions["US.QQQ"]
    assert pos["qty"] == 20.0
    # Avg cost should be approx 401
    assert pos["cost"] == 401.0
