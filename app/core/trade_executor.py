"""
交易执行器 - 带人工确认模式

流程:
信号生成 → 风控检查 → 推送钉钉 → 等待确认 → 执行/跳过
                                    ↓
                            15 分钟超时 → 标记过期

状态机:
pending → confirmed → executed
        → ignored
        → expired (15 分钟超时)
        → rejected (风控拒绝)
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
        1. 调用富途 OpenD API 下单
        2. 更新订单状态
        3. 记录交易到数据库
        4. 更新信号状态为 executed
        
        Args:
            order: 订单对象
            
        Returns:
            bool: 执行是否成功
        """
        try:
            logger.info(f"执行订单：{order.id} - {order.code} {order.action.value} {order.quantity}股 @ {order.price}")
            
            # 1. 调用富途 API 下单
            # TODO: 集成富途 OpenD API
            # 这里使用模拟实现，实际部署时需要替换为真实 API 调用
            
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
    
    def _submit_futu_order(self, order: Order) -> Optional[str]:
        """
        提交订单到富途 OpenD
        
        TODO: 实际部署时需要实现真实的富途 API 调用
        这里使用模拟实现用于测试
        
        Returns:
            str: 富途订单 ID，失败返回 None
        """
        # TODO: 集成富途 OpenD API
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
