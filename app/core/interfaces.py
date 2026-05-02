"""
Interfaces — 业务层接口定义

目的：通过 Protocol 定义接口，实现业务层与基础设施层的解耦。
所有模块通过接口通信，不依赖具体实现。

设计原则：
1. 依赖倒置：高层模块依赖接口，不依赖具体实现
2. 接口隔离：每个接口职责单一
3. 实现替换：模拟盘和实盘实现不同，但接口相同
"""

from abc import ABC, abstractmethod
from typing import Protocol, Optional, Dict, Any, List
from dataclasses import dataclass
from enum import Enum
from datetime import datetime


# ===== 领域模型 =====

@dataclass
class TradeSignal:
    """交易信号"""
    code: str
    action: str  # "BUY" | "SELL" | "SHORT" | "COVER"
    price: float
    quantity: int
    strategy: str
    confidence: float
    timestamp: Optional[datetime] = None
    metadata: Optional[Dict[str, Any]] = None


@dataclass
class Position:
    """持仓"""
    code: str
    quantity: int
    entry_price: float
    current_price: float
    entry_time: datetime
    market: str
    metadata: Optional[Dict[str, Any]] = None


@dataclass
class OrderResult:
    """订单执行结果"""
    success: bool
    order_id: str
    futu_order_id: Optional[str] = None
    executed_price: Optional[float] = None
    executed_quantity: Optional[int] = None
    error_message: Optional[str] = None


class OrderStatus(Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    FILLED = "filled"
    PARTIAL_FILLED = "partial_filled"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass
class TradeRecord:
    """交易记录"""
    code: str
    action: str
    price: float
    quantity: int
    timestamp: datetime
    order_id: str
    futu_order_id: Optional[str] = None
    close_futu_order_id: Optional[str] = None
    pnl: Optional[float] = None
    status: str = "open"
    strategy: Optional[str] = None


# ===== 接口定义 =====

class INotifier(Protocol):
    """通知器接口"""
    
    def send(self, title: str, content: str, **kwargs) -> bool:
        """发送通知"""
        ...
    
    def send_trade_open(self, trade: TradeRecord) -> bool:
        """发送开仓通知"""
        ...
    
    def send_trade_exit(self, trade: TradeRecord, reason: str) -> bool:
        """发送平仓通知"""
        ...
    
    def send_alert(self, level: str, message: str) -> bool:
        """发送告警"""
        ...


class ITradeExecutor(Protocol):
    """交易执行器接口"""
    
    def submit_order(self, signal: TradeSignal) -> OrderResult:
        """提交订单"""
        ...
    
    def close_position(self, position: Position, reason: str) -> OrderResult:
        """平仓"""
        ...
    
    def cancel_order(self, order_id: str) -> bool:
        """撤单"""
        ...
    
    def get_positions(self, market: Optional[str] = None) -> List[Position]:
        """获取持仓列表"""
        ...
    
    def get_orders(self, status: Optional[OrderStatus] = None) -> List[Dict[str, Any]]:
        """获取订单列表"""
        ...


class IRiskManager(Protocol):
    """风控管理器接口"""
    
    def pre_trade_check(self, signal: TradeSignal) -> bool:
        """交易前置检查"""
        ...
    
    def monitor_position(self, position: Position) -> Optional[str]:
        """监控持仓，返回退出原因或 None"""
        ...
    
    def check_circuit_breaker(self) -> bool:
        """检查熔断状态，返回是否允许交易"""
        ...
    
    def get_max_positions(self) -> int:
        """获取最大持仓数"""
        ...


class ITradeStore(Protocol):
    """交易仓储接口"""
    
    def save_trade(self, record: TradeRecord) -> bool:
        """保存交易记录"""
        ...
    
    def update_trade(self, record: TradeRecord) -> bool:
        """更新交易记录"""
        ...
    
    def get_open_positions(self, market: Optional[str] = None) -> List[Position]:
        """获取未平仓持仓"""
        ...
    
    def get_trade_history(self, code: Optional[str] = None, 
                          days: int = 30) -> List[TradeRecord]:
        """获取交易历史"""
        ...
    
    def get_trade_by_order_id(self, order_id: str) -> Optional[TradeRecord]:
        """根据订单 ID 查询交易记录"""
        ...


class IDataProvider(Protocol):
    """数据提供者接口"""
    
    def get_quote(self, code: str) -> Dict[str, Any]:
        """获取行情数据"""
        ...
    
    def get_kline(self, code: str, period: str, count: int) -> List[Dict[str, Any]]:
        """获取 K 线数据"""
        ...
    
    def subscribe(self, codes: List[str]) -> bool:
        """订阅行情"""
        ...
    
    def unsubscribe(self, codes: List[str]) -> bool:
        """退订行情"""
        ...


class IPositionManager(Protocol):
    """持仓管理器接口"""
    
    def open_position(self, code: str, price: float, quantity: int, 
                      market: str, **kwargs) -> Position:
        """开仓"""
        ...
    
    def close_position(self, code: str, price: float, close_time: str) -> bool:
        """平仓"""
        ...
    
    def get_position(self, code: str) -> Optional[Position]:
        """获取持仓"""
        ...
    
    def get_all_positions(self) -> List[Position]:
        """获取所有持仓"""
        ...


# ===== 事件定义 =====

class TradeEvent:
    """交易事件"""
    ORDER_SUBMITTED = "order.submitted"
    ORDER_FILLED = "order.filled"
    ORDER_CANCELLED = "order.cancelled"
    ORDER_FAILED = "order.failed"
    POSITION_OPENED = "position.opened"
    POSITION_CLOSED = "position.closed"
    POSITION_STOPPED = "position.stopped"
    SIGNAL_GENERATED = "signal.generated"
    SIGNAL_REJECTED = "signal.rejected"
    CIRCUIT_BREAKER_TRIPPED = "circuit_breaker.trip"
    RISK_ALERT = "risk.alert"
