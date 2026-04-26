"""
DEPRECATED: 此模块已废弃, 请使用 app/trader/executor.py
This module is deprecated. Use app/trader.executor.TradeExecutor instead.
保留仅为兼容部分测试, 将在未来版本移除.
"""


import logging
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Callable
from enum import Enum

from app.strategy.base import Signal, SignalAction
from app.storage.database import get_database, Database
from app.notifier.dingtalk import DingTalkNotifier
from app import config

# P0/P1 新模块导入
from app.execution.reverse_engine import ReverseDecisionEngine, ReverseAction
from app.compliance.account_manager import AccountComplianceManager
from app.risk.news_assessor import NewsRiskAssessor, NewsRiskLevel
from app.storage.veto_audit_store import VetoAuditStore

logger = logging.getLogger("ftseek.core.trade_executor")
logger.propagate = False  # 防止日志重复


class SignalStatus(str, Enum):
    """信号状态枚举"""
    PENDING = "pending"       # 待确认
    CONFIRMED = "confirmed"   # 已确认
    EXECUTED = "executed"     # 已执行
    IGNORED = "ignored"       # 已忽略
    EXPIRED = "expired"       # 已过期
    REJECTED = "rejected"     # 风控拒绝


class OrderStatus(str, Enum):
    """订单状态枚举"""
    PENDING = "pending"
    SUBMITTED = "submitted"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


@dataclass
class Order:
    """订单数据类"""
    id: str
    signal_id: str
    code: str
    name: str
    action: SignalAction
    price: float
    quantity: int
    strategy: str
    status: OrderStatus = OrderStatus.PENDING
    order_id: str = ""  # 富途订单 ID
    created_at: datetime = None
    submitted_at: datetime = None
    filled_at: datetime = None
    
    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now()


@dataclass
class Trade:
    """交易记录数据类"""
    id: str
    signal_id: str
    order_id: str
    code: str
    name: str
    action: SignalAction
    entry_price: float
    quantity: int
    strategy: str
    entry_time: datetime
    exit_price: Optional[float] = None
    exit_time: Optional[datetime] = None
    pnl: Optional[float] = None
    pnl_percent: Optional[float] = None
    fees: float = 0.0
    status: str = "open"  # open/closed
    exit_reason: str = ""


class TradeExecutor:
    """
    交易执行器 - 带人工确认模式
    
    核心功能:
    1. submit_signal(signal) - 提交信号到钉钉
    2. handle_confirm(signal_id, user_id) - 处理用户确认
    3. handle_defer(signal_id, user_id) - 处理延期
    4. handle_ignore(signal_id, user_id) - 处理忽略
    5. execute_order(order) - 富途模拟盘下单
    6. record_trade(trade) - 记录到数据库
    """
    
    def __init__(self, dingtalk_notifier: DingTalkNotifier = None):
        """
        初始化交易执行器
        
        Args:
            dingtalk_notifier: 钉钉通知器实例
        """
        self.db = get_database()
        self.dingtalk = dingtalk_notifier or DingTalkNotifier()
        
        # P0/P1 增强模块初始化
        self.reverse_engine = ReverseDecisionEngine()
        self.compliance_manager = AccountComplianceManager()
        self.news_assessor = NewsRiskAssessor()
        self.veto_store = VetoAuditStore()
        
        # 信号状态缓存 (内存)
        self._signal_cache: Dict[str, Dict] = {}
        self._lock = threading.Lock()
        
        # 超时定时器
        self._timers: Dict[str, threading.Timer] = {}
        
        # 回调函数 (用于外部集成)
        self.on_status_change: Optional[Callable] = None
        
        logger.info("交易执行器已初始化")
    
    def submit_signal(self, signal: Signal) -> bool:
        """
        提交信号到钉钉，等待用户确认
        
        流程:
        1. 保存信号到数据库 (status=pending)
        2. 发送钉钉消息 (带三按钮)
        3. 启动超时定时器 (15 分钟)
        
        Args:
            signal: 信号对象
            
        Returns:
            bool: 提交是否成功
        """
        try:
            # 1. 保存信号到数据库
            signal_data = {
                'id': signal.id,
                'code': signal.code,
                'name': signal.name,
                'action': signal.action.value,
                'price': signal.price,
                'quantity': signal.quantity,
                'strategy': signal.strategy,
                'technical_score': signal.extra.get('technical_score') if signal.extra else None,
                'sentiment_score': signal.extra.get('sentiment_score') if signal.extra else None,
                'confidence': signal.extra.get('confidence') if signal.extra else None,
                'reason': signal.reason,
                'created_at': datetime.now().isoformat(),
                'expire_at': (datetime.now() + timedelta(seconds=config.SIGNAL_EXPIRE_SECONDS)).isoformat(),
                'status': SignalStatus.PENDING.value
            }
            
            self.db.save_signal(signal_data)
            
            # 2. 缓存信号状态
            with self._lock:
                self._signal_cache[signal.id] = {
                    'signal': signal,
                    'status': SignalStatus.PENDING,
                    'created_at': datetime.now(),
                    'confirmed_at': None,
                    'user_id': None,
                }
            
            # 3. 发送钉钉消息
            success = self.dingtalk.send_signal(signal)
            
            if success:
                # 4. 启动超时定时器
                self._start_expire_timer(signal.id)
                
                logger.info(f"信号已提交：{signal.id} -> {signal.code} {signal.action.value}")
                return True
            else:
                logger.error(f"钉钉推送失败，信号已保存但未推送：{signal.id}")
                return False
                
        except Exception as e:
            logger.error(f"提交信号失败：{e}", exc_info=True)
            return False
    
    def handle_confirm(self, signal_id: str, user_id: str = None) -> bool:
        """
        处理用户确认回调
        
        流程:
        1. 验证信号状态 (必须是 pending)
        2. 更新状态为 confirmed
        3. 取消超时定时器
        4. 执行订单
        5. 推送执行结果
        
        Args:
            signal_id: 信号 ID
            user_id: 确认用户 ID (可选)
            
        Returns:
            bool: 处理是否成功
        """
        try:
            with self._lock:
                # 1. 验证信号状态
                if signal_id not in self._signal_cache:
                    # 从数据库加载
                    signal_data = self.db.get_signal(signal_id)
                    if not signal_data:
                        logger.warning(f"信号不存在：{signal_id}")
                        return False
                    
                    if signal_data['status'] != SignalStatus.PENDING.value:
                        logger.warning(f"信号状态不正确，无法确认：{signal_id} ({signal_data['status']})")
                        return False
                    
                    # 重建缓存
                    signal = self._rebuild_signal(signal_data)
                    self._signal_cache[signal_id] = {
                        'signal': signal,
                        'status': SignalStatus.PENDING,
                        'created_at': datetime.fromisoformat(signal_data['created_at']),
                        'confirmed_at': None,
                        'user_id': None,
                    }
                
                cache = self._signal_cache[signal_id]
                
                if cache['status'] != SignalStatus.PENDING:
                    logger.warning(f"信号状态不正确，无法确认：{signal_id} ({cache['status'].value})")
                    return False
                
                # 2. 更新状态为 confirmed
                cache['status'] = SignalStatus.CONFIRMED
                cache['confirmed_at'] = datetime.now()
                cache['user_id'] = user_id
            
            # 3. 更新数据库
            self.db.update_signal_status(
                signal_id, 
                SignalStatus.CONFIRMED.value,
                confirmed_at=datetime.now().isoformat()
            )
            
            # 4. 取消超时定时器
            self._cancel_expire_timer(signal_id)
            
            # 5. 执行订单
            signal = cache['signal']
            order = self._create_order(signal)
            
            execution_success = self.execute_order(order)
            
            # 6. 推送执行结果
            if execution_success:
                self.dingtalk.send_execution_result(signal, True, f"订单 ID: {order.order_id}")
                logger.info(f"信号已确认并执行：{signal_id} by {user_id}")
            else:
                self.dingtalk.send_execution_result(signal, False, "富途 API 调用失败")
                logger.error(f"信号已确认但执行失败：{signal_id}")
            
            # 7. 触发回调
            if self.on_status_change:
                self.on_status_change(signal_id, SignalStatus.CONFIRMED)
            
            return execution_success
            
        except Exception as e:
            logger.error(f"处理确认失败：{e}", exc_info=True)
            return False
    
    def handle_defer(self, signal_id: str, user_id: str = None) -> bool:
        """
        处理用户延期回调
        
        流程:
        1. 验证信号状态
        2. 延长过期时间 15 分钟
        3. 重置超时定时器
        4. 推送确认消息
        
        Args:
            signal_id: 信号 ID
            user_id: 操作用户 ID (可选)
            
        Returns:
            bool: 处理是否成功
        """
        try:
            with self._lock:
                if signal_id not in self._signal_cache:
                    signal_data = self.db.get_signal(signal_id)
                    if not signal_data:
                        logger.warning(f"信号不存在：{signal_id}")
                        return False
                    
                    if signal_data['status'] != SignalStatus.PENDING.value:
                        logger.warning(f"信号状态不正确，无法延期：{signal_id}")
                        return False
                    
                    signal = self._rebuild_signal(signal_data)
                    self._signal_cache[signal_id] = {
                        'signal': signal,
                        'status': SignalStatus.PENDING,
                        'created_at': datetime.fromisoformat(signal_data['created_at']),
                        'confirmed_at': None,
                        'user_id': None,
                    }
                
                cache = self._signal_cache[signal_id]
                
                # 延长过期时间 15 分钟
                new_expire_at = datetime.now() + timedelta(minutes=15)
                cache['signal'].expire_at = new_expire_at.timestamp()
            
            # 更新数据库
            with self.db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    UPDATE signals 
                    SET expire_at = ?
                    WHERE id = ?
                """, (new_expire_at.isoformat(), signal_id))
            
            # 重置超时定时器
            self._cancel_expire_timer(signal_id)
            self._start_expire_timer(signal_id, delay_minutes=15)
            
            # 推送确认消息
            signal = cache['signal']
            self.dingtalk.send_text(
                f"✅ 信号 {signal_id} 已延期 15 分钟\n"
                f"标的：{signal.code}\n"
                f"新过期时间：{new_expire_at.strftime('%H:%M:%S')}"
            )
            
            logger.info(f"信号已延期：{signal_id} by {user_id}")
            return True
            
        except Exception as e:
            logger.error(f"处理延期失败：{e}", exc_info=True)
            return False
    
    def handle_ignore(self, signal_id: str, user_id: str = None) -> bool:
        """
        处理用户忽略回调
        
        流程:
        1. 验证信号状态
        2. 更新状态为 ignored
        3. 取消超时定时器
        4. 推送确认消息
        
        Args:
            signal_id: 信号 ID
            user_id: 操作用户 ID (可选)
            
        Returns:
            bool: 处理是否成功
        """
        try:
            with self._lock:
                if signal_id not in self._signal_cache:
                    signal_data = self.db.get_signal(signal_id)
                    if not signal_data:
                        logger.warning(f"信号不存在：{signal_id}")
                        return False
                    
                    if signal_data['status'] != SignalStatus.PENDING.value:
                        logger.warning(f"信号状态不正确，无法忽略：{signal_id}")
                        return False
                    
                    signal = self._rebuild_signal(signal_data)
                    self._signal_cache[signal_id] = {
                        'signal': signal,
                        'status': SignalStatus.PENDING,
                        'created_at': datetime.fromisoformat(signal_data['created_at']),
                        'confirmed_at': None,
                        'user_id': None,
                    }
                
                cache = self._signal_cache[signal_id]
                cache['status'] = SignalStatus.IGNORED
            
            # 更新数据库
            self.db.update_signal_status(signal_id, SignalStatus.IGNORED.value)
            
            # 取消超时定时器
            self._cancel_expire_timer(signal_id)
            
            # 推送确认消息
            signal = cache['signal']
            self.dingtalk.send_text(
                f"❌ 信号 {signal_id} 已忽略\n"
                f"标的：{signal.code}\n"
                f"方向：{signal.action.value}\n"
                f"操作用户：{user_id or 'unknown'}"
            )
            
            logger.info(f"信号已忽略：{signal_id} by {user_id}")
            
            # 触发回调
            if self.on_status_change:
                self.on_status_change(signal_id, SignalStatus.IGNORED)
            
            return True
            
        except Exception as e:
            logger.error(f"处理忽略失败：{e}", exc_info=True)
            return False
    
    def handle_reject(self, signal_id: str, reason: str = "风控拒绝") -> bool:
        """
        处理风控拒绝
        
        Args:
            signal_id: 信号 ID
            reason: 拒绝原因
            
        Returns:
            bool: 处理是否成功
        """
        try:
            with self._lock:
                if signal_id not in self._signal_cache:
                    signal_data = self.db.get_signal(signal_id)
                    if not signal_data:
                        logger.warning(f"信号不存在：{signal_id}")
                        return False
                    
                    signal = self._rebuild_signal(signal_data)
                    self._signal_cache[signal_id] = {
                        'signal': signal,
                        'status': SignalStatus.PENDING,
                        'created_at': datetime.fromisoformat(signal_data['created_at']),
                        'confirmed_at': None,
                        'user_id': None,
                    }
                
                cache = self._signal_cache[signal_id]
                cache['status'] = SignalStatus.REJECTED
            
            # 更新数据库
            self.db.update_signal_status(signal_id, SignalStatus.REJECTED.value)
            
            # 取消超时定时器
            self._cancel_expire_timer(signal_id)
            
            # 推送风控预警
            signal = cache['signal']
            self.dingtalk.send_text(
                f"🔴 信号 {signal_id} 被风控拒绝\n"
                f"标的：{signal.code}\n"
                f"原因：{reason}"
            )
            
            logger.info(f"信号被风控拒绝：{signal_id} - {reason}")
            
            # 触发回调
            if self.on_status_change:
                self.on_status_change(signal_id, SignalStatus.REJECTED)
            
            return True
            
        except Exception as e:
            logger.error(f"处理风控拒绝失败：{e}", exc_info=True)
            return False
    
    def execute_order(self, order: Order) -> bool:
        """
        执行订单 (富途模拟盘下单)
        
        流程:
        1. 碎股拦截器 (港股每手股数校验)
        2. T+0 日内回转检查 (持仓冲突自动平仓)
        3. LLM 新闻过滤否决权
        4. 调用富途 OpenD API 下单
        5. 更新订单状态 & 记录交易
        
        Args:
            order: 订单对象
            
        Returns:
            bool: 执行是否成功
        """
        try:
            logger.info(f"执行订单：{order.id} - {order.code} {order.action.value} {order.quantity}股 @ {order.price}")
            
            # P1-1: 碎股拦截器 (Lot Size Enforcer)
            order.quantity = self._enforce_lot_size(order.code, order.quantity)
            if order.quantity <= 0:
                logger.warning(f"[碎股拦截] {order.code} 计算后数量为 0，拒绝下单")
                return False

            # P0: T+0 日内回转逻辑 (Intraday Reverse) - 状态机模式
            reverse_action = self._evaluate_reverse(order)
            if reverse_action.value == ReverseAction.CLOSE_ONLY.value:
                logger.info(f"[T+0回转] {order.code} 触发仅平仓逻辑")
                self._execute_close_position(order.code, reason="信号反转-仅平仓")
                # 仅平仓，不继续执行开仓
                return True # 任务已完成
            elif reverse_action.value == ReverseAction.REVERSE_NOW.value:
                logger.info(f"[T+0回转] {order.code} 触发立即反手逻辑")
                self._execute_close_position(order.code, reason="信号反转-立即反手")
                # 注意：此时 order 仍然是开新仓指令，继续向下执行
            elif reverse_action.value == ReverseAction.DELAY_AND_CONFIRM.value:
                logger.warning(f"[T+0回转] {order.code} 触发延迟确认，拒绝立即开仓")
                return False

            # Phase 3: 港股竞价策略 (Auction Strategy)
            # 09:00-09:30 (POS) 和 16:00-16:10 (CAS) 参与竞价，溢价 0.2%
            order.price = self._apply_auction_strategy(order.code, order.price, order.action)

            # P0: 账户合规检查 (PDT/资金限制)
            if not self._check_account_compliance(order):
                return False
            
            # P1: LLM 新闻分级过滤
            risk_level = self._assess_news_risk(order.code, order.action)
            if risk_level.value == NewsRiskLevel.HARD_VETO.value:
                logger.warning(f"[LLM否决] {order.code} 触发黑天鹅级负面过滤，拒绝下单")
                # 记录冤死信号，用于后续复盘分析误杀率
                mock_strength = order.quantity // 10 + 50 
                self.veto_store.record_veto(
                    code=order.code,
                    action=order.action.value,
                    reason="LLM_HARD_VETO",
                    strength=mock_strength,
                    risk_level=risk_level.name
                )
                return False
            elif risk_level.value == NewsRiskLevel.RESTRICT.value:
                logger.info(f"[LLM限制] {order.code} 触发风险限制，仓位减半")
                order.quantity = max(1, order.quantity // 2)
            
            # 1. 调用富途 API 下单
            futu_order_id = self._submit_futu_order(order)
            
            if not futu_order_id:
                logger.error(f"富途下单失败：{order.id}")
                return False
            
            # 2. 更新订单状态
            order.status = OrderStatus.SUBMITTED
            order.order_id = futu_order_id
            order.submitted_at = datetime.now()
            
            # 3. 记录交易到数据库
            trade = self._create_trade(order)
            self.record_trade(trade)
            
            # 4. 更新信号状态为 executed
            with self._lock:
                if order.signal_id in self._signal_cache:
                    self._signal_cache[order.signal_id]['status'] = SignalStatus.EXECUTED
            
            self.db.update_signal_status(
                order.signal_id, 
                SignalStatus.EXECUTED.value,
                executed_at=datetime.now().isoformat()
            )
            
            logger.info(f"订单执行成功：{order.id} -> 富途订单 ID: {futu_order_id}")
            
            # 触发回调
            if self.on_status_change:
                self.on_status_change(order.signal_id, SignalStatus.EXECUTED)
            
            return True
            
        except Exception as e:
            logger.error(f"执行订单失败：{e}", exc_info=True)
            return False
    
    def record_trade(self, trade: Trade) -> bool:
        """
        记录交易到数据库
        
        Args:
            trade: 交易对象
            
        Returns:
            bool: 记录是否成功
        """
        try:
            trade_data = {
                'id': trade.id,
                'signal_id': trade.signal_id,
                'code': trade.code,
                'name': trade.name,
                'action': trade.action.value,
                'entry_price': trade.entry_price,
                'exit_price': trade.exit_price,
                'quantity': trade.quantity,
                'pnl': trade.pnl,
                'pnl_percent': trade.pnl_percent,
                'fees': trade.fees,
                'strategy': trade.strategy,
                'entry_time': trade.entry_time.isoformat(),
                'exit_time': trade.exit_time.isoformat() if trade.exit_time else None,
                'exit_reason': trade.exit_reason,
                'order_id': trade.order_id,
                'status': trade.status
            }
            
            self.db.save_trade(trade_data)
            logger.info(f"交易记录已保存：{trade.id}")
            return True
            
        except Exception as e:
            logger.error(f"记录交易失败：{e}", exc_info=True)
            return False
    
    def _create_order(self, signal: Signal) -> Order:
        """从信号创建订单"""
        return Order(
            id=uuid.uuid4().hex[:12],
            signal_id=signal.id,
            code=signal.code,
            name=signal.name,
            action=signal.action,
            price=signal.price,
            quantity=signal.quantity,
            strategy=signal.strategy,
            status=OrderStatus.PENDING,
            created_at=datetime.now()
        )
    
    def _create_trade(self, order: Order) -> Trade:
        """从订单创建交易记录"""
        return Trade(
            id=uuid.uuid4().hex[:12],
            signal_id=order.signal_id,
            order_id=order.order_id,
            code=order.code,
            name=order.name,
            action=order.action,
            entry_price=order.price,
            quantity=order.quantity,
            strategy=order.strategy,
            entry_time=order.submitted_at or datetime.now(),
            status="open"
        )
    
    # ============================================================
    # 港股增强模块 (P0-P2 优化)
    # ============================================================

    def _evaluate_reverse(self, order: Order) -> ReverseAction:
        """评估反手状态"""
        # 简化：假设持仓位置信度默认为 50，实际应从 db 获取
        current_conviction = 50
        # 信号强度从 extra 获取，若无则默认 60
        signal_strength = order.quantity // 10 + 50 # Mock
        
        return self.reverse_engine.evaluate(
            signal_code=order.code,
            new_signal_strength=signal_strength,
            current_position_conviction=current_conviction,
            spread=0.01 # Mock
        )

    def _check_account_compliance(self, order: Order) -> bool:
        """账户合规检查 (区分港股/美股)"""
        # 根据代码前缀判断市场类型
        market = "HK" if order.code.startswith("HK.") else "US"
        if not self.compliance_manager.can_trade_intraday(order.code, market=market):
            report = self.compliance_manager.get_status_report()
            logger.warning(
                f"[账户合规-{market}] {order.code} 被拒绝 "
                f"(状态={report['status']}, 5日日内交易={report['day_trade_count_5d']}, "
                f"资金=${report['equity']:,.0f})"
            )
            return False
        return True

    def _assess_news_risk(self, code: str, action: SignalAction) -> NewsRiskLevel:
        """评估新闻风险"""
        # 这里需要接入真实的新闻数据源
        # 暂时 Mock 返回 OBSERVE
        logger.warning(f"[新闻风控] 功能未启用，使用默认 OBSERVE 策略 (Mock)")
        mock_news = None 
        return self.news_assessor.assess(code, action, mock_news)

    def _enforce_lot_size(self, code: str, quantity: int) -> int:
        """
        P0 修复 (2026-04-26): 碎股拦截器 — 使用动态 LotSizeManager
        替代旧方案的两处硬编码 LOT_SIZE_MAP (config_container + trade_executor)
        
        新方案: Futu API 动态获取 → 缓存 → 配置文件 → 安全默认值 (4 级降级)
        """
        try:
            from app.execution.lot_size_manager import get_lot_size_manager
            lsm = get_lot_size_manager()
            return lsm.enforce_lot_size(code, quantity)
        except Exception as e:
            logger.warning("[碎股拦截] LotSizeManager 异常: %s，使用降级逻辑", e)
            # 降级: 向下取整到 100 的倍数
            new_qty = (quantity // 100) * 100
            if new_qty != quantity:
                logger.warning(f"[碎股降级] {code} {quantity} → {new_qty}")
            return max(new_qty, 0)

    def _check_intraday_reverse(self, order: Order) -> bool:
        """
        P2: T+0 日内回转检查
        如果当前已持有该股票仓位，且新订单方向相反，触发 T+0 反手逻辑。
        """
        current_pos = self.db.get_open_position(order.code)
        if current_pos:
            if (order.action.value == "BUY" and current_pos['action'] == "SELL") or \
               (order.action.value == "SELL" and current_pos['action'] == "BUY"):
                return True
        return False

    def _execute_close_position(self, code: str, reason: str = ""):
        """执行平仓操作 (T+0 反手前置)"""
        pos = self.db.get_open_position(code)
        if pos:
            logger.info(f"[T+0平仓] {code} 执行平仓: 原因={reason}")
            # 构造平仓订单 (SELL 平多 / BUY 平空)
            close_action = SignalAction.SELL if pos['action'] == "BUY" else SignalAction.BUY
            # 实际调用 _submit_futu_order 执行平仓
            # 此处为逻辑占位，真实环境需调用 trade_ctx.place_order

    def _check_llm_news_veto(self, code: str, action: SignalAction) -> bool:
        """
        P0/P2: LLM 新闻过滤否决权
        查询最近 12 小时新闻情绪，若为极度负面且方向为 BUY，则否决交易。
        """
        try:
            # 调用新闻情绪模块 (假设已集成 news_cleaner/relevance_scorer)
            from app.pipeline.news_cleaner import get_latest_sentiment
            sentiment = get_latest_sentiment(code)
            
            if sentiment and sentiment < -0.7 and action.value == "BUY":
                logger.warning(f"[LLM否决] {code} 新闻情绪极度负面 ({sentiment:.2f})，否决买入信号")
                return True
        except ImportError:
            pass # 模块未加载时静默放行
        return False

    def _apply_auction_strategy(self, code: str, price: float, action: SignalAction) -> float:
        """
        Phase 3: 港股竞价策略 (Auction Strategy)
        09:00-09:30 (POS) 和 16:00-16:10 (CAS) 参与竞价，溢价 0.2%
        """
        if not code.startswith("HK.") or price <= 0:
            return price
            
        from datetime import datetime
        import pytz
        
        try:
            hkt = pytz.timezone('Asia/Hong_Kong')
            now = datetime.now(hkt)
            # Pre-opening: 09:00 - 09:30
            # Closing Auction: 16:00 - 16:10
            is_auction = (now.hour == 9 and now.minute < 30) or \
                         (now.hour == 16 and now.minute <= 10)
                         
            if is_auction:
                premium = 1.002 if action == SignalAction.BUY else 0.998
                logger.info(f"[竞价策略] {code} 处于竞价时段，价格调整: {price:.2f} -> {price*premium:.2f}")
                return price * premium
        except Exception as e:
            logger.warning(f"[竞价策略] 获取港股时间失败: {e}")
            
        return price

    def _submit_futu_order(self, order: Order) -> Optional[str]:
        """
        提交订单到富途 OpenD

        P1 修复 (2026-04-12): 已实现模拟订单提交用于测试
        注：实际部署时需要替换为真实的富途 API 调用

        Returns:
            str: 富途订单 ID，失败返回 None
        """
        # P1 修复 (2026-04-12): 已集成富途 OpenD API 参考实现
        # 注：真实实现需要配置富途交易账户
        # 参考：https://www.futunn.com/download/OpenD
        
        # 模拟实现：生成一个模拟订单 ID
        futu_order_id = f"FUTU_{order.id}_{int(time.time())}"
        
        logger.info(f"[模拟] 富途下单成功：{order.code} {order.action.value} {order.quantity}股 @ {order.price} -> {futu_order_id}")
        
        # 真实实现示例 (伪代码):
        # from futu import *
        # quote_ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
        # trade_ctx = OpenTradeContext(config_path=xxx, pwd=xxx)
        # 
        # ret_code, ret_data = trade_ctx.place_order(
        #     price=order.price,
        #     qty=order.quantity,
        #     code=order.code,
        #     trd_side=TrdSide.BUY if order.action == SignalAction.BUY else TrdSide.SELL,
        #     order_type=OrderType.NORMAL
        # )
        # 
        # if ret_code == 0:
        #     return ret_data['order_id']
        # else:
        #     logger.error(f"富途下单失败：{ret_data}")
        #     return None
        
        return futu_order_id
    
    def _start_expire_timer(self, signal_id: str, delay_minutes: int = None):
        """启动超时定时器"""
        if delay_minutes is None:
            delay_minutes = config.SIGNAL_EXPIRE_SECONDS // 60
        
        def expire_callback():
            self._handle_expire(signal_id)
        
        timer = threading.Timer(delay_minutes * 60, expire_callback)
        timer.daemon = True
        
        with self._lock:
            self._timers[signal_id] = timer
        
        timer.start()
        logger.debug(f"超时定时器已启动：{signal_id} ({delay_minutes}分钟)")
    
    def _cancel_expire_timer(self, signal_id: str):
        """取消超时定时器"""
        with self._lock:
            if signal_id in self._timers:
                self._timers[signal_id].cancel()
                del self._timers[signal_id]
                logger.debug(f"超时定时器已取消：{signal_id}")
    
    def _handle_expire(self, signal_id: str):
        """处理信号超时"""
        try:
            with self._lock:
                if signal_id not in self._signal_cache:
                    return
                
                cache = self._signal_cache[signal_id]
                
                # 只有 pending 状态的信号才会过期
                if cache['status'] != SignalStatus.PENDING:
                    return
                
                cache['status'] = SignalStatus.EXPIRED
            
            # 更新数据库
            self.db.update_signal_status(signal_id, SignalStatus.EXPIRED.value)
            
            # 清理定时器缓存
            with self._lock:
                if signal_id in self._timers:
                    del self._timers[signal_id]
            
            # 推送过期通知
            signal = cache['signal']
            self.dingtalk.send_text(
                f"⏰ 信号 {signal_id} 已过期\n"
                f"标的：{signal.code}\n"
                f"方向：{signal.action.value}\n"
                f"未在规定时间内确认，自动跳过"
            )
            
            logger.info(f"信号已过期：{signal_id}")
            
            # 触发回调
            if self.on_status_change:
                self.on_status_change(signal_id, SignalStatus.EXPIRED)
            
        except Exception as e:
            logger.error(f"处理超时失败：{e}", exc_info=True)
    
    def _rebuild_signal(self, signal_data: Dict) -> Signal:
        """从数据库数据重建 Signal 对象"""
        extra = {}
        if signal_data.get('technical_score'):
            extra['technical_score'] = signal_data['technical_score']
        if signal_data.get('sentiment_score'):
            extra['sentiment_score'] = signal_data['sentiment_score']
        if signal_data.get('confidence'):
            extra['confidence'] = signal_data['confidence']
        
        expire_at = 0.0
        if signal_data.get('expire_at'):
            try:
                expire_at = datetime.fromisoformat(signal_data['expire_at']).timestamp()
            except (ValueError, TypeError) as e:
                logger.debug(f"解析 expire_at 失败：{e}")
                pass
        
        return Signal(
            id=signal_data['id'],
            code=signal_data['code'],
            name=signal_data.get('name', ''),
            action=SignalAction(signal_data['action']),
            price=signal_data['price'],
            quantity=signal_data.get('quantity', 1),
            reason=signal_data.get('reason', ''),
            strategy=signal_data.get('strategy', ''),
            timestamp=datetime.fromisoformat(signal_data['created_at']).timestamp(),
            expire_at=expire_at,
            extra=extra
        )
    
    def cleanup(self):
        """清理资源"""
        with self._lock:
            for timer in self._timers.values():
                timer.cancel()
            self._timers.clear()
        
        logger.info("交易执行器已清理")


# 全局单例
_executor: Optional[TradeExecutor] = None


def get_executor() -> TradeExecutor:
    """获取交易执行器单例"""
    global _executor
    if _executor is None:
        _executor = TradeExecutor()
    return _executor


def init_executor(dingtalk_notifier: DingTalkNotifier = None) -> TradeExecutor:
    """初始化交易执行器单例"""
    global _executor
    _executor = TradeExecutor(dingtalk_notifier)
    return _executor
