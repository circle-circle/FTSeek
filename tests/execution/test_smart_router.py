# tests/execution/test_smart_router.py
import pytest
from app.execution.smart_router import SmartRouter
from app.core.models import Order, Direction, OrderType

def test_smart_router_selects_broker():
    """测试智能路由器选择券商"""
    router = SmartRouter()
    
    # 添加模拟券商
    router.add_broker("futuu", priority=1, available=True)
    router.add_broker("ibkr", priority=2, available=True)
    
    # 应该选择优先级最高的可用券商
    broker = router.select_broker("AAPL")
    assert broker == "futuu"

def test_smart_router_fallback():
    """测试路由器故障转移"""
    router = SmartRouter()
    
    router.add_broker("futuu", priority=1, available=False)  # 不可用
    router.add_broker("ibkr", priority=2, available=True)
    
    broker = router.select_broker("AAPL")
    assert broker == "ibkr"
