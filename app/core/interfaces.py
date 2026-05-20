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
from typing import Protocol, Optional, Dict, Any, List, Tuple
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


# ===== Provider 接口 (2026-05-19: 架构级重构 Phase 0) =====
# 用于 risk_pipeline 依赖倒置，替代对 DataSubscriber 私有属性的直接访问

class ITradingHoursProvider(Protocol):
    """交易时段 Provider — 提供交易时间判断"""

    def is_trading_hours(self) -> bool:
        """是否在交易时段"""
        ...

    def is_high_quality_trading_hours(self) -> bool:
        """是否在高质量交易时段 (非开盘/收盘噪音时段)"""
        ...

    def get_trading_period(self) -> Dict[str, str]:
        """获取当前交易时段名称 (如 'morning_open', 'midday', 'afternoon')"""
        ...


class IExitStateProvider(Protocol):
    """退出状态 Provider — 提供止损/止盈/追踪 状态查询"""

    def get_trailing_exit_time(self, code: str) -> float:
        """获取标的追踪止盈触发时间戳 (0=未触发)"""
        ...

    def get_trailing_stop_price(self, code: str) -> float:
        """获取标的追踪止损价格 (0=未设置)"""
        ...

    def get_peak_price(self, code: str) -> float:
        """获取标的峰值价格缓存 (0=无缓存)"""
        ...


class ICooldownProvider(Protocol):
    """冷却管理 Provider — 提供信号/开仓/标的冷却查询"""

    def is_signal_cooled(self, code: str) -> bool:
        """信号是否已冷却 (距离上次信号足够久)"""
        ...

    def get_last_open_ts(self, code: str) -> float:
        """获取标的最近一次开仓时间戳 (0=从未开仓)"""
        ...

    def get_symbol_loss_cooldown_until(self, code: str) -> float:
        """获取标的连续亏损冷却截止时间戳 (0=未冷却)"""
        ...


class ICircuitBreakerProvider(Protocol):
    """智能熔断 Provider — 提供账户级熔断状态查询"""

    def is_paused(self) -> bool:
        """熔断器是否处于暂停状态"""
        ...

    def check(self, current_equity: Optional[float], signal_confidence: float) -> Tuple[bool, str, float]:
        """智能熔断检查 → (allowed, reason, position_multiplier)"""
        ...

    def get_current_equity(self) -> Optional[float]:
        """获取当前账户权益"""
        ...


class IQualityProvider(Protocol):
    """质量管理器 Provider — 提供标的质量检查"""

    def check_quality(self, code: str, price: float) -> Optional[Dict]:
        """检查标的质量和估值 → 返回质量报告字典或 None"""
        ...

    def get_quality_scorer(self) -> Optional[Any]:
        """获取质量评分器实例 (None=未启用)"""
        ...


class IMarketDataProvider(Protocol):
    """行情数据 Provider — 提供 ADX/ATR/MTF 指标查询"""

    def get_mtf_alignment(self, code: str, action: str) -> Tuple:
        """获取多周期趋势对齐结果 → (confirmed, boost, reason)"""
        ...

    def get_mtf_scores(self, code: str) -> Tuple[float, float]:
        """获取 MTF BUY/SELL 对齐分数 → (buy_score, sell_score)"""
        ...

    def update_mtf_alignment(self, code: str, buy_score: float, sell_score: float) -> None:
        """同步 MTF 分数至共识引擎缓存"""
        ...

    def get_adx(self, code: str) -> float:
        """获取标的 ADX 值 (0=无数据)"""
        ...


class IPositionQueryProvider(Protocol):
    """持仓查询 Provider — 提供只读持仓查询"""

    def get_position(self, code: str) -> Optional[Any]:
        """获取指定标的持仓 (None=无持仓)"""
        ...

    def get_all_positions(self) -> List[Any]:
        """获取所有持仓列表"""
        ...

    def get_open_position_ts(self, code: str) -> float:
        """获取开仓时间戳 (同 get_last_open_ts, 语义别名)"""
        ...
