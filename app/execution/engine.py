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
        
        # 持仓缓存 (由 scheduler_core 注入, 用于替代 Futu API 方向冲突检查)
        self.position_store = None
    
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
    
    # ── 方案B: 标的级互斥 (2026-06-03) ──────────────────────────────────
    # 每个标的永久归属一个引擎，物理上消除方向冲突。
    # ORB 只能交易这 8 只 ETF，Main/Scalping 绝不碰它们。
    # 来源: unified_config.yaml::orb.universe (SSOT)
    # 注释: SPY/QQQ 在主线 watchlist 中，ORB 已移除避免冲突
    ORB_SYMBOLS = frozenset({
        "US.IWM", "US.DIA", "US.XLE", "US.XLF",
        "US.XLK", "US.XLV", "US.XLY", "US.XLI",
    })

    @classmethod
    def _get_engine_source(cls, order: "Order") -> str:
        """从 client_order_id 前缀识别订单来源引擎"""
        cid = getattr(order, "client_order_id", "") or ""
        if cid.startswith("orb_"):
            return "ORB"
        return "MAIN"

    @classmethod
    def _is_exit_order(cls, order: "Order") -> bool:
        """退出/平仓订单不参与标的互斥校验"""
        if getattr(order, "metadata", None) and order.metadata.get("is_exit"):
            return True
        cid = getattr(order, "client_order_id", "") or ""
        if cid.startswith("exit_"):
            return True
        # 冲突平仓单也是退出性质
        if "_CLOSE_SHORT_" in cid or "_CLOSE_LONG_" in cid:
            return True
        return False

    def _apply_sim_slippage(self, order: "Order") -> None:
        """P1.1 (RFC-060): 模拟盘成交价施加现实方向性滑点.

        原模拟: fill_price = order.price (零滑点 → 策略盈利假象, 实盘往往亏损).
        现: BUY 成交价上浮 (吃 ask), SELL 下浮 (吃 bid), 幅度 = base_bps + depth_cost_bps.
        方向推断: 未平仓 LONG=BUY / SHORT=SELL; 平仓 LONG→SELL / SHORT→BUY.
        就地修改 order.fill_price; 滑点 bps 记入 order.metadata 供归因.
        配置 (config): simulate_slippage_bps / simulate_depth_cost_bps (默认 5/2 bps).
        """
        cfg = self.config or {}
        base_bps = float(cfg.get("simulate_slippage_bps", 5.0))
        depth_bps = float(cfg.get("simulate_depth_cost_bps", 2.0))
        total_bps = base_bps + depth_bps
        if total_bps <= 0 or not getattr(order, "price", None):
            order.fill_price = order.price
            return
        is_exit = self._is_exit_order(order)
        dir_val = getattr(getattr(order, "direction", None), "value", "") or ""
        # 推断订单 side (BUY/SELL): 未平仓 LONG/SHORT→BUY/SELL; 平仓反转
        if dir_val == "LONG":
            is_buy = not is_exit
        elif dir_val == "SHORT":
            is_buy = is_exit
        else:
            order.fill_price = order.price
            return
        slip = total_bps / 10000.0
        mult = (1 + slip) if is_buy else (1 - slip)
        order.fill_price = round(order.price * mult, 2)
        try:
            md = getattr(order, "metadata", None) or {}
            md["sim_slippage_bps"] = round(total_bps, 1)
            md["sim_is_buy"] = is_buy
            if hasattr(order, "metadata"):
                order.metadata = md
        except Exception:
            pass

    def _check_symbol_ownership(self, order: "Order") -> bool:
        """方案B: 标的级互斥校验。返回 True=放行, False=REJECT。

        规则:
          - ORB 引擎只能交易 ORB_SYMBOLS (8只ETF)
          - Main/Scalping 不能交易 ORB_SYMBOLS
          - 退出订单(is_exit)不受此限制
        """
        if self._is_exit_order(order):
            return True

        symbol = getattr(order, "symbol", "")
        if not symbol:
            return True  # 无标的信息，保守放行

        engine = self._get_engine_source(order)
        is_orb_symbol = symbol in self.ORB_SYMBOLS

        if engine == "ORB" and not is_orb_symbol:
            logger.error(
                f"[标的互斥🔴] ORB 引擎试图交易非 ORB 标的 {symbol} → REJECTED "
                f"(ORB 只允许: {', '.join(sorted(self.ORB_SYMBOLS))})"
            )
            return False

        if engine == "MAIN" and is_orb_symbol:
            logger.error(
                f"[标的互斥🔴] {engine} 引擎试图交易 ORB 标的 {symbol} → REJECTED "
                f"(该标的归属 ORB 引擎)"
            )
            return False

        return True

    def submit_order(self, order: Order) -> Order:
        """
        提交订单
        
        流程：
        1. 标的互斥校验 (方案B)
        2. 幂等检查
        3. 选择订单类型
        4. 选择券商
        5. 提交到券商
        6. 记录状态
        """
        # ── 方案B: 标的级互斥校验 (最先执行) ──
        if not self._check_symbol_ownership(order):
            order.state = OrderState.REJECTED
            order.failure_reason = f"标的互斥: {order.symbol} 不属于 {self._get_engine_source(order)} 引擎"
            return order

        # ── P0 通用安全闸门 (2026-06-14): 所有引擎的统一关卡 ──
        # 退出订单始终放行 (紧急平仓不可阻断), 入场订单受 kill_switch + auto_trading 控制
        if not self._is_exit_order(order):
            # 1. Kill Switch — 最高优先级, 阻断一切入场
            try:
                from app.us_pipeline.emergency_interceptor import check_kill_switch
                if check_kill_switch():
                    order.state = OrderState.REJECTED
                    order.failure_reason = "Kill Switch 激活, 所有入场订单被阻断"
                    logger.critical(
                        f"[ExecutionEngine·KillSwitch] {order.symbol} 入场被阻断 "
                        f"(source={self._get_engine_source(order)})"
                    )
                    return order
            except Exception as _ks_err:
                # P1.2 (RFC-060): fail-close — Kill Switch 是最高优先级紧急制动,
                # 检查本身异常时, 安全起见拒绝入场 (原 fail-open 会静默绕过紧急停止)
                logger.critical(
                    f"[ExecutionEngine·KillSwitch] {order.symbol} kill switch 检查异常, "
                    f"fail-close 拒绝入场: {_ks_err}"
                )
                order.state = OrderState.REJECTED
                order.failure_reason = f"Kill Switch 检查异常 (fail-close): {_ks_err}"
                return order

            # 2. auto_trading_enabled — 阻断 ORB/Main 自动引擎的入场
            #    手动订单 (client_order_id 以 "manual_" 开头) 不受此限制
            _source = self._get_engine_source(order)
            _cid = getattr(order, "client_order_id", "") or ""
            _is_manual = _cid.startswith("manual_")
            if _source in ("ORB", "MAIN") and not _is_manual:
                try:
                    import re as _re
                    import os as _os
                    _yaml_path = _os.path.join(
                        _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))),
                        "config", "unified_config.yaml"
                    )
                    _auto_enabled = False
                    with open(_yaml_path, "r") as _f:
                        for _line in _f:
                            _m = _re.match(r'\s+auto_trading_enabled:\s*(true|false)', _line)
                            if _m:
                                _auto_enabled = _m.group(1) == 'true'
                                break
                    if not _auto_enabled:
                        order.state = OrderState.REJECTED
                        order.failure_reason = (
                            f"auto_trading_enabled=false, {_source} 引擎入场被阻断"
                        )
                        logger.warning(
                            f"[ExecutionEngine·AutoGate] {order.symbol} {_source} 入场被阻断 "
                            f"(auto_trading_enabled=false)"
                        )
                        return order
                except Exception:
                    pass  # YAML 读取异常不阻断 (order_executor 有自己的独立检查)

        # 幂等检查
        # ------------------------------------------------------------------
        # Critical 修复 2026-06-02: 退出订单与开仓订单的幂等命名空间隔离
        #
        # 历史问题：
        #   旧实现仅靠 is_duplicate(bypass=True) 跳过去重，但 get_order_id()
        #   分支与重复检测耦合在同一 if 中。一旦 bypass 失效或 client_order_id
        #   命中旧开仓键，就会返回旧 order_id，导致退出/平仓订单未真正下发，
        #   资金与仓位被锁定。
        #
        # 修复策略：
        #   1) 一旦识别为 is_exit 订单，整个 “是否重复 → 取旧订单” 分支完全不执行；
        #   2) 给退出订单的 client_order_id 加 'exit_' 前缀，从根本上把开仓与平仓
        #      的幂等空间隔离开（即使 record_order 使用 INSERT OR IGNORE，
        #      不同前缀也不会互相覆盖）；
        #   3) 兼容模拟盘 (TRADE_ENV=1) 与实盘，行为一致。
        # ------------------------------------------------------------------
        is_exit_order = bool(
            getattr(order, 'metadata', None)
            and order.metadata.get('is_exit', False)
        )

        if is_exit_order:
            # 命名空间隔离：避免与同 symbol 的开仓 client_order_id 冲突
            if not order.client_order_id.startswith("exit_"):
                order.client_order_id = f"exit_{order.client_order_id}"
            logger.info(
                f"[ExecutionEngine] 退出订单跳过幂等去重 symbol={order.symbol} "
                f"client_order_id={order.client_order_id} (强制创建新订单)"
            )
        else:
            # 开仓订单：保持既有幂等保护不变
            if self.idempotency_manager.is_duplicate(order.client_order_id):
                existing_order_id = self.idempotency_manager.get_order_id(order.client_order_id)
                if existing_order_id and existing_order_id in self.order_book:
                    logger.warning(
                        f"[ExecutionEngine] 检测到重复开仓订单，返回已存在订单 "
                        f"client_order_id={order.client_order_id} order_id={existing_order_id}"
                    )
                    return self.order_book[existing_order_id]
                raise ValueError(f"Duplicate order: {order.client_order_id}")
            
            # P0 修复 (2026-06-03): 通用方向冲突检测 — 所有入场订单的统一关卡
            # 根因: ORB / main pipeline 各自独立下单，
            #       同一标的被多引擎同时操作 → broker 净仓位方向与系统认知不一致
            # 规则: 如果已有该标的持仓，禁止新入场 (无论同向或反向)
            #       必须先平掉旧仓再开新仓，由调用方负责
            # P1 性能修复 (2026-06-06): 用 position_store 内存查询替代 Futu position_list_query
            #       原: 每次入场调 Futu API (50-200ms 网络延迟)
            #       现: position_store.has() 内存查询 (<1ms)，30s sync 刷新足够
            _ps = getattr(self, 'position_store', None)
            if _ps and _ps.has(order.symbol):
                existing = _ps.get(order.symbol)
                existing_dir = existing.direction.value if hasattr(existing.direction, 'value') else str(existing.direction)
                order_side = 'LONG' if order.direction == Direction.LONG else 'SHORT'
                logger.error(
                    f"[ExecutionEngine·方向冲突🔴] {order.symbol} "
                    f"已有 {existing_dir} {existing.size:.0f}股 (position_store), "
                    f"新入场 {order_side} 被拦截 (先平旧仓再开新仓)"
                )
                order.state = OrderState.REJECTED
                order.failure_reason = f"方向冲突: 已有{existing_dir}持仓"
                return order
        
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
                    self._apply_sim_slippage(order)  # P1.1: 方向性滑点 (原 fill_price=order.price 零滑点)
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
                # 无 broker 连接 — 区分模拟/实盘
                is_sim = self.config.get("futu_trade_env", 1) == 1
                simulate_realistic = self.config.get("simulate_realistic_flow", True)

                # 🔴 P0 (2026-06-13): 实盘无 broker 时必须 REJECT, 绝不允许假装成交
                if not is_sim:
                    order.state = OrderState.REJECTED
                    order.failure_reason = "实盘模式下 broker 未连接, 拒绝下单"
                    state_machine.transition(OrderState.REJECTED)
                    logger.error(f"[ExecutionEngine] 🔴 REJECTED: {order.symbol} — broker 未连接, 实盘拒绝模拟成交")
                    return order

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
                    self._apply_sim_slippage(order)  # P1.1: 方向性滑点 (原 fill_price=order.price 零滑点)
                    order.filled_quantity = order.quantity
                    logger.debug(f"[ExecutionEngine] 模拟成交 (离线·完整链路): {order.symbol} {order.direction.value} @ ${order.fill_price:.2f}")
                else:
                    # 兼容模式: 直接标记 FILLED
                    order.state = OrderState.FILLED
                    state_machine.transition(OrderState.FILLED)
                    order.sent_at = now_et()
                    order.filled_at = now_et()
                    self._apply_sim_slippage(order)  # P1.1: 方向性滑点 (原 fill_price=order.price 零滑点)
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
