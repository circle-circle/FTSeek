"""
执行引擎主类

集成所有执行模块，提供统一的订单执行接口
"""
from typing import Dict, Any, Optional, List
from datetime import datetime
import uuid

from app.core.engine import ExecutionEngine as BaseExecutionEngine
from app.core.models import Order, OrderState, SignalScore, TradeEvent, FailureRecord, FailureType, Direction, OrderType
from app.core.events import EventStore
from app.utils.timezone import now_et

from .order_state_machine import OrderStateMachine
from .idempotency import IdempotencyManager
from .order_type_selector import OrderTypeSelector
from .smart_router import SmartRouter
from .order_retry import OrderRetryManager
from .slippage_control import SlippageController
from .broker_adapter import FutuBrokerAdapter

import logging
logger = logging.getLogger(__name__)


class ExecutionEngine(BaseExecutionEngine):
    """
    执行引擎主类
    
    集成所有执行模块：
    - OrderStateMachine: 订单状态管理
    - IdempotencyManager: 幂等控制
    - OrderTypeSelector: 订单类型选择
    - SmartRouter: 智能路由
    - OrderRetryManager: 重试机制
    - SlippageController: 滑点控制
    - FutuBrokerAdapter: 券商适配器
    """
    
    def __init__(self, config: Dict[str, Any], event_store: EventStore):
        super().__init__(config, event_store)
        
        # 执行模块
        self.order_state_machine: Optional[OrderStateMachine] = None
        self.idempotency_manager: Optional[IdempotencyManager] = None
        self.order_type_selector: Optional[OrderTypeSelector] = None
        self.smart_router: Optional[SmartRouter] = None
        self.order_retry_manager: Optional[OrderRetryManager] = None
        self.slippage_controller: Optional[SlippageController] = None
        self.broker_adapter: Optional[FutuBrokerAdapter] = None
        
        # 订单缓存
        self.order_book: Dict[str, Order] = {}
        self.state_machines: Dict[str, OrderStateMachine] = {}
    
    def initialize(self):
        """初始化执行引擎"""
        # 初始化各模块
        idempotency_window = self.config.get("idempotency_window", 3600)
        self.idempotency_manager = IdempotencyManager(window_seconds=idempotency_window)
        
        # 订单类型选择器配置
        market_min_score = self.config.get("market_order_min_score", 80)
        limit_max_score = self.config.get("limit_order_max_score", 60)
        twap_min_value = self.config.get("twap_order_min_value", 100000)
        self.order_type_selector = OrderTypeSelector(
            market_order_min_score=market_min_score,
            limit_order_max_score=limit_max_score,
            twap_order_min_value=twap_min_value,
        )
        
        # 智能路由器
        self.smart_router = SmartRouter()
        
        # 配置券商
        brokers = self.config.get("brokers", [])
        for broker_cfg in brokers:
            self.smart_router.add_broker(
                name=broker_cfg.get("name", "futu"),
                priority=broker_cfg.get("priority", 1),
                available=broker_cfg.get("available", True),
            )
        
        # 重试管理器
        max_attempts = self.config.get("max_retry_attempts", 3)
        retry_delay = self.config.get("retry_delay_seconds", 5)
        self.order_retry_manager = OrderRetryManager(
            max_attempts=max_attempts,
            delay_seconds=retry_delay,
        )
        
        # 滑点控制器
        max_slippage = self.config.get("max_slippage_ratio", 0.005)
        self.slippage_controller = SlippageController(max_slippage_ratio=max_slippage)
        
        # 券商适配器
        futu_host = self.config.get("futu_host", "127.0.0.1")
        futu_port = self.config.get("futu_port", 11111)
        futu_env = self.config.get("futu_trade_env", 1)
        futu_account_id = self.config.get("futu_account_id", "")
        self.broker_adapter = FutuBrokerAdapter(
            host=futu_host,
            port=futu_port,
            trade_env=futu_env,
            account_id=futu_account_id,
        )
        
        # 连接券商
        if self.config.get("auto_connect_broker", False):
            self.broker_adapter.connect()
        
        self.enabled = True
        self.emit_event(TradeEvent(
            event_id=str(uuid.uuid4()),
            event_type="ENGINE_INITIALIZED",
            timestamp=datetime.now(),
            source=self.__class__.__name__,
            data={"engine": "ExecutionEngine"},
        ))
    
    def shutdown(self):
        """关闭执行引擎"""
        self.enabled = False
        if self.broker_adapter:
            self.broker_adapter.disconnect()
        self.order_book.clear()
        self.state_machines.clear()
        self.emit_event(TradeEvent(
            event_id=str(uuid.uuid4()),
            event_type="ENGINE_SHUTDOWN",
            timestamp=datetime.now(),
            source=self.__class__.__name__,
            data={"engine": "ExecutionEngine"},
        ))
    
    def submit_order(self, order: Order) -> Order:
        """
        提交订单
        
        流程：
        1. 幂等检查
        2. 选择订单类型
        3. 选择券商
        4. 提交到券商
        5. 记录状态
        """
        # 幂等检查
        # P1-2 修复 2026-04-26: 风控/退出订单 (metadata.is_exit=True) 跳过幂等拦截
        is_exit_order = getattr(order, 'metadata', None) and order.metadata.get('is_exit', False)
        if self.idempotency_manager.is_duplicate(order.client_order_id, bypass=bool(is_exit_order)):
            existing_order_id = self.idempotency_manager.get_order_id(order.client_order_id)
            if existing_order_id and existing_order_id in self.order_book:
                return self.order_book[existing_order_id]
            raise ValueError(f"Duplicate order: {order.client_order_id}")
        
        # 创建状态机
        state_machine = OrderStateMachine()
        self.state_machines[order.order_id] = state_machine
        
        # 选择订单类型（如果未指定）
        if order.order_type == OrderType.LIMIT and hasattr(order, 'signal_id') and order.signal_id:
            # 如果有信号，可以根据信号选择订单类型
            pass
        
        # 选择券商
        broker = self.smart_router.select_broker(order.symbol)
        if not broker:
            raise RuntimeError("No available broker")
        order.broker = broker

        # 更新状态：CREATED -> VALIDATED
        state_machine.transition(OrderState.VALIDATED)
        order.state = OrderState.VALIDATED

        # 记录幂等
        self.idempotency_manager.record_order(order.client_order_id, order.order_id)

        # 提交到券商
        try:
            if self.broker_adapter and self.broker_adapter.is_connected():
                # P0 修复 (2026-05-11): 模拟盘走完整状态链路，确保与实盘行为一致
                # 配置 simulate_realistic_flow 控制是否走完整链路 (默认 True)
                is_sim = self.config.get("futu_trade_env", 1) == 1
                simulate_realistic = self.config.get("simulate_realistic_flow", True)

                if is_sim and not simulate_realistic:
                    # 兼容模式: 旧逻辑直接标记 FILLED (不推荐)
                    result = self.broker_adapter.submit_order(order)
                    if isinstance(result, dict) and result.get("futu_order_id"):
                        order.futu_order_id = result["futu_order_id"]
                    order.state = OrderState.FILLED
                    state_machine.transition(OrderState.FILLED)
                    order.sent_at = now_et()
                    order.filled_at = now_et()
                    order.fill_price = order.price
                    order.filled_quantity = order.quantity
                    logger.debug(f"[ExecutionEngine] 模拟成交 (兼容模式): {order.symbol} {order.direction.value} @ ${order.fill_price:.2f}")
                elif is_sim and simulate_realistic:
                    # 完整链路模式: 模拟盘也走 CREATED → VALIDATED → SENT → ACKED → FILLED
                    result = self.broker_adapter.submit_order(order)
                    if isinstance(result, dict) and result.get("futu_order_id"):
                        order.futu_order_id = result["futu_order_id"]

                    # SENT
                    order.state = OrderState.SENT
                    state_machine.transition(OrderState.SENT)
                    order.sent_at = now_et()

                    # 模拟 ACKED (延迟 configurable)
                    ack_delay = float(self.config.get("simulate_fill_delay_seconds", 0.0))
                    if ack_delay > 0:
                        import time
                        time.sleep(ack_delay)

                    # ACKED → FILLED (模拟盘自动撮合)
                    order.state = OrderState.ACKED
                    state_machine.transition(OrderState.ACKED)

                    # 模拟部分成交检测 (可选，测试 PARTIALLY_FILLED 路径)
                    simulate_partial = self.config.get("simulate_partial_fill", False)
                    if simulate_partial and order.quantity > 1:
                        # 测试部分成交: 先成交一半
                        order.state = OrderState.PARTIALLY_FILLED
                        state_machine.transition(OrderState.PARTIALLY_FILLED)
                        order.filled_quantity = order.quantity // 2
                        order.avg_fill_price = order.price
                        logger.debug(f"[ExecutionEngine] 模拟部分成交: {order.symbol} {order.filled_quantity}/{order.quantity} @ ${order.price:.2f}")
                        # 然后完成剩余
                        order.filled_quantity = order.quantity

                    order.state = OrderState.FILLED
                    state_machine.transition(OrderState.FILLED)
                    order.filled_at = now_et()
                    order.fill_price = order.price
                    order.filled_quantity = order.quantity
                    logger.debug(f"[ExecutionEngine] 模拟成交 (完整链路): {order.symbol} {order.direction.value} @ ${order.fill_price:.2f}")
                else:
                    # 实盘：走正常 SENT 路径
                    result = self.broker_adapter.submit_order(order)
                    if isinstance(result, dict) and result.get("futu_order_id"):
                        order.futu_order_id = result["futu_order_id"]
                    order.state = OrderState.SENT
                    state_machine.transition(OrderState.SENT)
                    order.sent_at = now_et()
            else:
                # 模拟模式直接成交 (无 broker 连接)
                # P0 修复 (2026-05-11): 也走完整链路
                is_sim = self.config.get("futu_trade_env", 1) == 1
                simulate_realistic = self.config.get("simulate_realistic_flow", True)

                if is_sim and simulate_realistic:
                    # 完整链路模式
                    order.state = OrderState.SENT
                    state_machine.transition(OrderState.SENT)
                    order.sent_at = now_et()

                    ack_delay = float(self.config.get("simulate_fill_delay_seconds", 0.0))
                    if ack_delay > 0:
                        import time
                        time.sleep(ack_delay)

                    # ACKED → [PARTIALLY_FILLED] → FILLED
                    order.state = OrderState.ACKED
                    state_machine.transition(OrderState.ACKED)

                    # 模拟部分成交检测 (可选，测试 PARTIALLY_FILLED 路径)
                    simulate_partial = self.config.get("simulate_partial_fill", False)
                    if simulate_partial and order.quantity > 1:
                        order.state = OrderState.PARTIALLY_FILLED
                        state_machine.transition(OrderState.PARTIALLY_FILLED)
                        order.filled_quantity = order.quantity // 2
                        order.avg_fill_price = order.price
                        logger.debug(f"[ExecutionEngine] 模拟部分成交 (离线): {order.symbol} {order.filled_quantity}/{order.quantity} @ ${order.price:.2f}")

                    order.state = OrderState.FILLED
                    state_machine.transition(OrderState.FILLED)
                    order.filled_at = now_et()
                    order.fill_price = order.price
                    order.filled_quantity = order.quantity
                    logger.debug(f"[ExecutionEngine] 模拟成交 (离线·完整链路): {order.symbol} {order.direction.value} @ ${order.fill_price:.2f}")
                else:
                    # 兼容模式: 直接标记 FILLED
                    order.state = OrderState.FILLED
                    state_machine.transition(OrderState.FILLED)
                    order.sent_at = now_et()
                    order.filled_at = now_et()
                    order.fill_price = order.price
                    order.filled_quantity = order.quantity
                    logger.debug(f"[ExecutionEngine] 模拟成交 (离线·兼容模式): {order.symbol} {order.direction.value} @ ${order.fill_price:.2f}")
        except Exception as e:
            order.state = OrderState.REJECTED
            order.last_error = str(e)
            state_machine.transition(OrderState.REJECTED)
            self.record_failure(FailureRecord(
                failure_id=str(uuid.uuid4()),
                failure_type=FailureType.EXEC_REJECTED,
                timestamp=datetime.now(),
                symbol=order.symbol,
                order_id=order.order_id,
                reason=str(e),
            ))
            raise
        
        # 存入订单簿
        self.order_book[order.order_id] = order
        
        # 发出事件
        self.emit_event(TradeEvent(
            event_id=str(uuid.uuid4()),
            event_type="ORDER_SUBMITTED",
            timestamp=datetime.now(),
            order_id=order.order_id,
            signal_id=order.signal_id,
            source=self.__class__.__name__,
            data={
                "symbol": order.symbol,
                "direction": order.direction.value,
                "quantity": order.quantity,
                "price": order.price,
                "broker": order.broker,
            },
        ))
        
        return order
    
    def cancel_order(self, order_id: str) -> bool:
        """
        取消订单
        
        流程：
        1. 检查订单是否存在
        2. 检查状态是否允许取消
        3. 调用券商取消
        4. 更新状态
        """
        if order_id not in self.order_book:
            raise ValueError(f"Order not found: {order_id}")
        
        order = self.order_book[order_id]
        
        if not order.is_active():
            raise ValueError(f"Order {order_id} is not active (state={order.state.value})")
        
        state_machine = self.state_machines.get(order_id)
        
        # 调用券商取消
        try:
            if self.broker_adapter and self.broker_adapter.is_connected():
                success = self.broker_adapter.cancel_order(order_id)
                if not success:
                    raise RuntimeError("Broker cancel failed")
            # 模拟模式：直接成功
        except Exception as e:
            order.last_error = str(e)
            self.record_failure(FailureRecord(
                failure_id=str(uuid.uuid4()),
                failure_type=FailureType.EXEC_CANCEL_FAILED,
                timestamp=datetime.now(),
                symbol=order.symbol,
                order_id=order_id,
                reason=str(e),
            ))
            raise
        
        # 更新状态
        order.state = OrderState.CANCELED
        order.canceled_at = datetime.now()
        if state_machine:
            state_machine.transition(OrderState.CANCELED)
        
        # 发出事件
        self.emit_event(TradeEvent(
            event_id=str(uuid.uuid4()),
            event_type="ORDER_CANCELED",
            timestamp=datetime.now(),
            order_id=order_id,
            source=self.__class__.__name__,
        ))
        
        return True
    
    def query_order(self, order_id: str) -> Order:
        """
        查询订单状态
        
        流程：
        1. 检查本地缓存
        2. 如果需要，查询券商
        3. 更新状态
        """
        if order_id not in self.order_book:
            raise ValueError(f"Order not found: {order_id}")
        
        order = self.order_book[order_id]
        
        # 查询券商获取最新状态
        try:
            if self.broker_adapter and self.broker_adapter.is_connected():
                broker_order = self.broker_adapter.query_order(order_id)
                if broker_order:
                    # 更新本地订单状态
                    order.state = broker_order.state
                    order.filled_quantity = broker_order.filled_quantity
                    order.avg_fill_price = broker_order.avg_fill_price
        except Exception as e:
            # 查询失败不影响返回本地状态
            order.last_error = str(e)
        
        return order
    
    def select_order_type(self, signal: SignalScore) -> OrderType:
        """选择订单类型"""
        if not self.order_type_selector:
            return OrderType.LIMIT
        return self.order_type_selector.select(signal)
    
    def optimize_execution(self, order: Order) -> Order:
        """
        优化执行策略
        
        可以根据市场状况、滑点等优化订单参数
        """
        # 这里可以实现更复杂的优化逻辑
        # 例如：根据市场波动率调整价格、拆分大单等
        return order
    
    def check_slippage(self, expected_price: float, actual_price: float, side: str) -> bool:
        """检查滑点是否可接受"""
        if not self.slippage_controller:
            return True
        return self.slippage_controller.is_acceptable(expected_price, actual_price, side)
    
    def should_retry(self, order_id: str) -> bool:
        """检查是否应该重试"""
        if not self.order_retry_manager:
            return False
        return self.order_retry_manager.should_retry(order_id)
    
    def record_retry(self, order_id: str):
        """记录重试"""
        if self.order_retry_manager:
            self.order_retry_manager.record_attempt(order_id)
    
    def get_order_state(self, order_id: str) -> Optional[OrderState]:
        """获取订单状态"""
        if order_id in self.state_machines:
            return self.state_machines[order_id].current_state
        if order_id in self.order_book:
            return self.order_book[order_id].state
        return None
    
    def get_active_orders(self) -> List[Order]:
        """获取所有活跃订单"""
        return [order for order in self.order_book.values() if order.is_active()]
    
    def get_completed_orders(self) -> List[Order]:
        """获取所有已完成订单"""
        return [order for order in self.order_book.values() if order.is_complete()]
