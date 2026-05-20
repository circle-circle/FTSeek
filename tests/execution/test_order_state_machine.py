# tests/execution/test_order_state_machine.py
import pytest
from app.execution.order_state_machine import OrderStateMachine
from app.core.models import OrderState

def test_order_state_transitions():
    """测试订单状态转换"""
    machine = OrderStateMachine()
    
    # 初始状态
    assert machine.current_state == OrderState.CREATED
    
    # CREATED → VALIDATED
    machine.transition(OrderState.VALIDATED)
    assert machine.current_state == OrderState.VALIDATED
    
    # VALIDATED → SENT
    machine.transition(OrderState.SENT)
    assert machine.current_state == OrderState.SENT
    
    # SENT → ACKED
    machine.transition(OrderState.ACKED)
    assert machine.current_state == OrderState.ACKED
    
    # ACKED → FILLED
    machine.transition(OrderState.FILLED)
    assert machine.current_state == OrderState.FILLED
    assert machine.is_complete() == True

def test_invalid_state_transition():
    """测试无效状态转换"""
    machine = OrderStateMachine()
    
    # CREATED 不能直接到 FILLED
    with pytest.raises(ValueError, match="Invalid transition"):
        machine.transition(OrderState.FILLED)
