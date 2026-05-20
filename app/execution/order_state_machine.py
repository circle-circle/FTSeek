"""
订单状态机

管理订单生命周期状态转换
"""
from typing import Set, Dict, List
from app.core.models import OrderState


class OrderStateMachine:
    """订单状态机"""
    
    # 定义有效的状态转换
    # P0 修复 (2026-04-17): VALIDATED 也允许转换到 REJECTED（券商下单失败场景）
    VALID_TRANSITIONS: Dict[OrderState, Set[OrderState]] = {
        OrderState.CREATED: {OrderState.VALIDATED, OrderState.CANCELED},
        OrderState.VALIDATED: {OrderState.SENT, OrderState.CANCELED, OrderState.REJECTED, OrderState.FILLED},
        OrderState.SENT: {OrderState.ACKED, OrderState.REJECTED, OrderState.CANCELED},
        OrderState.ACKED: {OrderState.PARTIALLY_FILLED, OrderState.FILLED, OrderState.CANCELED},
        OrderState.PARTIALLY_FILLED: {OrderState.FILLED, OrderState.CANCELED},
        OrderState.FILLED: set(),  # 终态
        OrderState.CANCELED: set(),  # 终态
        OrderState.REJECTED: set(),  # 终态
        OrderState.EXPIRED: set(),  # 终态
        OrderState.UNKNOWN: set(),  # 终态
    }
    
    def __init__(self):
        self.current_state = OrderState.CREATED
        self.history: List[OrderState] = [OrderState.CREATED]
    
    def transition(self, new_state: OrderState):
        """转换到新状态"""
        valid_next_states = self.VALID_TRANSITIONS.get(self.current_state, set())
        
        if new_state not in valid_next_states:
            raise ValueError(
                f"Invalid transition from {self.current_state.value} to {new_state.value}. "
                f"Valid transitions: {[s.value for s in valid_next_states]}"
            )
        
        self.current_state = new_state
        self.history.append(new_state)
    
    def is_complete(self) -> bool:
        """订单是否已完成"""
        return self.current_state in {
            OrderState.FILLED,
            OrderState.CANCELED,
            OrderState.REJECTED,
            OrderState.EXPIRED,
        }
    
    def is_active(self) -> bool:
        """订单是否仍活跃"""
        return self.current_state in {
            OrderState.CREATED,
            OrderState.VALIDATED,
            OrderState.SENT,
            OrderState.ACKED,
            OrderState.PARTIALLY_FILLED,
        }
    
    def reset(self):
        """重置状态机"""
        self.current_state = OrderState.CREATED
        self.history = [OrderState.CREATED]
