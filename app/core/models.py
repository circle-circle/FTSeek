"""
FTSeek-US 核心数据模型

定义四大引擎共享的数据结构
"""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, List, Dict, Any
import uuid

from app.utils.timezone import now_et


class EntryReason(str, Enum):
    """入场原因枚举 (2026-05-30: 支持归因系统 entry_reason 推导)"""
    MAIN_SIGNAL = "main_signal"
    CROSS_MARKET = "cross_market"
    NEWS_DRIVEN = "news_driven"
    EARNINGS = "earnings"
    ORB_BREAKOUT = "orb_breakout"         # 开盘区间突破
    FAST_LANE = "fast_lane"               # 作战计划快车道
    TREND_FIRST = "trend_first"           # 趋势回调
    MANUAL = "manual"
    UNKNOWN = "unknown"


def derive_entry_reason(strategy_id: str = "", source: str = "", metadata: Optional[Dict] = None) -> str:
    """从持仓元数据推导入场原因 (2026-05-30: 修复导入缺失)

    优先级:
      1. metadata['entry_reason'] 已有值直接返回
      2. 从 source_type / source 推导
      3. 从 strategy_id 推导
      4. 兜底 'unknown'
    """
    if metadata is None:
        metadata = {}

    # 优先从 metadata 直接获取
    existing = metadata.get('entry_reason', '')
    if existing and existing != 'None':
        return existing

    # 从 source 推导
    src = source or metadata.get('source_type', '') or metadata.get('source', '')
    if src == 'MAIN_PIPELINE':
        return metadata.get('signal_source', EntryReason.MAIN_SIGNAL.value)
    elif src == 'CROSS_MARKET':
        return EntryReason.CROSS_MARKET.value
    elif src == 'NEWS':
        return EntryReason.NEWS_DRIVEN.value

    # 从 strategy_id 推导
    if strategy_id:
        _sid = strategy_id.lower()
        if 'orb' in _sid:
            return EntryReason.ORB_BREAKOUT.value
        elif 'fast_lane' in _sid or 'battle_plan' in _sid:
            return EntryReason.FAST_LANE.value
        elif 'trend_first' in _sid:
            return EntryReason.TREND_FIRST.value
        elif 'cross' in _sid or 'arb' in _sid:
            return EntryReason.CROSS_MARKET.value
        elif 'news' in _sid:
            return EntryReason.NEWS_DRIVEN.value
        elif 'earn' in _sid:
            return EntryReason.EARNINGS.value
        elif 'voting_llm' in _sid or 'llm_vote' in _sid:
            return EntryReason.MAIN_SIGNAL.value
        elif 'main' in _sid:
            return EntryReason.MAIN_SIGNAL.value
        return strategy_id

    return src or EntryReason.UNKNOWN.value


class Direction(str, Enum):
    """交易方向"""
    LONG = "LONG"
    SHORT = "SHORT"


class OrderType(str, Enum):
    """订单类型"""
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    IOC = "IOC"  # Immediate or Cancel
    FOK = "FOK"  # Fill or Kill
    TWAP = "TWAP"
    VWAP = "VWAP"


class OrderState(str, Enum):
    """订单状态机"""
    CREATED = "CREATED"
    VALIDATED = "VALIDATED"
    SENT = "SENT"
    ACKED = "ACKED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


class MarketRegime(str, Enum):
    """市场状态"""
    TREND_UP = "TREND_UP"        # 强上涨
    TREND_DOWN = "TREND_DOWN"    # 强下跌
    RANGE = "RANGE"              # 震荡
    HIGH_VOL = "HIGH_VOL"        # 高波动
    LOW_VOL = "LOW_VOL"          # 低波动


class ExitReason(str, Enum):
    """退出原因"""
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    TRAILING_STOP = "TRAILING_STOP"
    TIME_STOP = "TIME_STOP"
    STRUCTURE_EXIT = "STRUCTURE_EXIT"
    MANUAL = "MANUAL"
    RISK_CUT = "RISK_CUT"
    POSITION_CONFLICT = "POSITION_CONFLICT"  # P0 新增: 持仓方向冲突平仓
    # ExitMonitor 专用 (2026-04-25 统一)
    FLASH_EXIT = "FLASH_EXIT"
    HARD_STOP = "HARD_STOP"
    BREAKEVEN_STOP = "BREAKEVEN_STOP"
    EMERGENCY = "EMERGENCY"  # P0-3 新增 2026-04-26: 紧急清仓 (EmergencyInterceptor 使用)
    SCALE_OUT = "SCALE_OUT"  # 2026-04-27: 分批止盈部分平仓
    PRE_CLOSE_EXIT = "PRE_CLOSE_EXIT"  # 2026-04-27: 盘末强制退出
    VELOCITY_EXIT = "VELOCITY_EXIT"  # 2026-05-31: 速度捕捉退出
    DAY_TRADE_TP = "DAY_TRADE_TP"  # 2026-06-03: 日内止盈 (day_trade 规则 TP 触发)
    DAY_TRADE_TIMEOUT = "DAY_TRADE_TIMEOUT"  # 2026-06-03: 日内超时退出 (day_trade 规则超时触发)
    MAX_HOLD_T5 = "MAX_HOLD_T5"  # RFC-061 (2026-06-24): cohort B 持仓≥5 交易日强平
    TIME_EXIT = "TIME_EXIT"  # RFC-061 (2026-06-24): 持仓>2h 且亏损保本退出
    OVERNIGHT_GAP_STOP = "OVERNIGHT_GAP_STOP"  # RFC-061 (2026-06-24) 快平桶; 2026-09-24 勘误: 实际判据 = 持仓过夜 且 亏损 ≥ overnight_gap_stop_pct, **无 gap 校验** (见 exit_engine 规则2) ⇒ "GAP" 只是历史命名, 不要按"跳空"解读或去查 gap% 字段。


class FailureType(str, Enum):
    """失败类型分类"""
    # 信号失败
    SIGNAL_EXPIRED = "signal_expired"
    SIGNAL_CONFLICT = "signal_conflict"
    SIGNAL_FILTERED = "signal_filtered"
    SIGNAL_DUPLICATE = "signal_duplicate"
    
    # 风控失败
    RISK_POSITION_LIMIT = "risk_position_limit"
    RISK_DRAWDOWN = "risk_drawdown"
    RISK_LEVERAGE = "risk_leverage"
    RISK_LIQUIDITY = "risk_liquidity"
    RISK_SCORE_LOW = "risk_score_low"
    
    # 执行失败
    EXEC_TIMEOUT = "exec_timeout"
    EXEC_REJECTED = "exec_rejected"
    EXEC_CANCEL_FAILED = "exec_cancel_failed"
    EXEC_PARTIAL_FILL = "exec_partial_fill"
    EXEC_SLIPPAGE = "exec_slippage"
    
    # 系统失败
    SYS_MARKET_DATA_DOWN = "sys_market_data_down"
    SYS_STORAGE_FAILED = "sys_storage_failed"
    SYS_DB_EXCEPTION = "sys_db_exception"
    SYS_API_EXCEPTION = "sys_api_exception"
    SYS_PROCESS_CRASH = "sys_process_crash"


class AlertLevel(str, Enum):
    """告警级别"""
    INFO = "INFO"
    WARN = "WARN"
    CRITICAL = "CRITICAL"


@dataclass
class SignalScore:
    """
    信号评分结构
    
    从"布尔值"升级为"评分值"
    """
    signal_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    trace_id: str = ""              # 全链路归因 ID (Phase 2)
    strategy_id: str = ""
    symbol: str = ""
    
    # 核心评分
    direction: Direction = Direction.LONG
    strength: int = 0              # 0-100 分
    confidence: float = 0.0        # 0.0-1.0 置信度
    
    # 市场适配性
    expected_horizon: int = 0      # 预计持有周期 (分钟)
    volatility_fit: bool = False   # 是否适合当前波动率
    market_regime: MarketRegime = MarketRegime.RANGE
    risk_reward_estimate: float = 0.0  # 预估盈亏比
    
    # 评分因子明细 (用于归因)
    score_breakdown: Dict[str, int] = field(default_factory=dict)
    # 示例: {"trend_confirm": 20, "volume_confirm": 15, "market_align": 15}
    
    # 时间戳
    created_at: datetime = field(default_factory=datetime.now)
    expires_at: Optional[datetime] = None
    
    # 元数据
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def is_expired(self) -> bool:
        """检查信号是否过期 — P1-3 (2026-05-14): 时区安全比较, 防 naive vs aware TypeError"""
        if self.expires_at is None:
            return False
        try:
            from app.utils.datetime_utils import is_expired_safe
            return is_expired_safe(self.expires_at)
        except Exception:
            return datetime.now() > self.expires_at  # fallback
    
    def get_position_ratio(self) -> float:
        """根据评分获取仓位比例"""
        if self.strength < 50:
            return 0.0
        elif self.strength < 70:
            return 0.25
        elif self.strength < 85:
            return 0.50
        else:
            return 1.0
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "signal_id": self.signal_id,
            "strategy_id": self.strategy_id,
            "symbol": self.symbol,
            "direction": self.direction.value,
            "strength": self.strength,
            "confidence": self.confidence,
            "expected_horizon": self.expected_horizon,
            "volatility_fit": self.volatility_fit,
            "market_regime": self.market_regime.value,
            "risk_reward_estimate": self.risk_reward_estimate,
            "score_breakdown": self.score_breakdown,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }


@dataclass
class Order:
    """
    订单对象
    
    支持完整生命周期管理
    """
    order_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    client_order_id: str = ""      # 幂等 ID
    signal_id: str = ""
    trace_id: str = ""             # 全链路归因 ID (Phase 2)
    
    symbol: str = ""
    direction: Direction = Direction.LONG
    order_type: OrderType = OrderType.LIMIT
    
    quantity: float = 0.0
    price: float = 0.0             # 限价单价格
    stop_price: float = 0.0        # 止损单触发价
    
    state: OrderState = OrderState.CREATED
    filled_quantity: float = 0.0
    avg_fill_price: float = 0.0
    
    # 执行信息
    broker: str = ""
    exchange: str = ""
    commission: float = 0.0
    slippage: float = 0.0

    # Futu 订单 ID (由 broker_adapter 写入)
    futu_order_id: str = ""
    
    # 时间戳
    created_at: datetime = field(default_factory=datetime.now)
    sent_at: Optional[datetime] = None
    filled_at: Optional[datetime] = None
    canceled_at: Optional[datetime] = None
    
    # 重试信息
    retry_count: int = 0
    last_error: Optional[str] = None
    
    # 元数据 (P0 2026-06-06: 支持 is_exit 等标记)
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def is_complete(self) -> bool:
        """订单是否已完成 (成交/取消/拒绝/过期)"""
        return self.state in [
            OrderState.FILLED,
            OrderState.CANCELED,
            OrderState.REJECTED,
            OrderState.EXPIRED,
        ]
    
    def is_active(self) -> bool:
        """订单是否仍活跃"""
        return self.state in [
            OrderState.CREATED,
            OrderState.VALIDATED,
            OrderState.SENT,
            OrderState.ACKED,
            OrderState.PARTIALLY_FILLED,
        ]


@dataclass
class Position:
    """
    持仓对象
    
    完整生命周期管理
    """
    position_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    trace_id: str = ""              # 全链路归因 ID (Phase 2)
    # R2 (2026-05-15): DEPRECATED — 用 owner_strategy 作为策略归属 SoT
    # strategy_id 保留写入仅为向后兼容旧 trade_attribution 数据, 新代码请用 owner_strategy
    strategy_id: str = ""
    signal_id: str = ""
    
    symbol: str = ""
    direction: Direction = Direction.LONG
    size: float = 0.0
    avg_entry_price: float = 0.0
    
    # 初始风控
    initial_sl: float = 0.0
    initial_tp: float = 0.0
    
    # 动态风控
    current_sl: float = 0.0        # 当前止损价 (移动止损)
    trail_state: Dict[str, Any] = field(default_factory=dict)
    
    # 退出配置
    take_profit_levels: List[Dict[str, float]] = field(default_factory=list)
    # [{ratio: 0.3, price: 100}, {ratio: 0.3, price: 110}, {ratio: 0.4, price: 120}]
    time_stop_hours: int = 72
    structure_exit_price: Optional[float] = None
    
    # 时间戳
    entry_time: datetime = field(default_factory=now_et)
    exit_time: Optional[datetime] = None
    
    # 退出信息
    exit_reason: Optional[ExitReason] = None
    exit_price: float = 0.0
    realized_pnl: float = 0.0

    # Futu 订单 ID (开仓时从 broker_adapter 写入，平仓时记录平仓单 ID)
    futu_order_id: str = ""
    
    # 归因
    market_regime: MarketRegime = MarketRegime.RANGE
    total_slippage: float = 0.0
    total_fees: float = 0.0
    
    # 加仓/减仓记录
    add_on_records: List[Dict[str, Any]] = field(default_factory=list)

    # Phase A (2026-05-14): 多策略归属
    # owner_strategy: "main" | "fast_lane" | "llm_plan"
    # 用于 ExitDispatcher 路由 + daily_review 按策略分组
    owner_strategy: Optional[str] = None
    # 仓位占总净值百分比 (用于 CapitalAllocator 释放)
    position_pct: float = 0.0

    # ─── MAE/MFE 跟踪 (2026-06-02: 反馈闭环修复) ───
    # Maximum Adverse Excursion: 持仓期间最大浮亏金额 (负数, 0=未记录)
    # 用于评估止损是否设得太紧/太松, 是止损优化的核心数据
    max_adverse_excursion: float = 0.0
    # Maximum Favorable Excursion: 持仓期间最大浮盈金额 (正数, 0=未记录)
    # 用于评估止盈是否设得太低 (MFE 远大于 TP 说明利润回吐过多)
    max_favorable_excursion: float = 0.0

    # 运行时元数据 (不持久化，用于异常标记等临时状态)
    metadata: Dict[str, Any] = field(default_factory=dict)

    # 分批止盈标记 (2026-06-06: 替代 trail_state 追踪, DB持久化到 open_positions.is_partially_reduced)
    is_partially_reduced: bool = False
    
    @property
    def unrealized_pnl(self) -> float:
        """计算未实现盈亏 (需要当前价格)"""
        return 0.0  # 由外部传入当前价格计算
    
    @property
    def holding_hours(self) -> float:
        """计算持仓时长 (小时) — 仅计算交易时段，跳过周末/节假日"""
        # P0 修复 (2026-05-31): 用 now_et() 替代 datetime.now() 统一时区
        try:
            from app.utils.timezone import now_et as _now_et
            end_time = self.exit_time or _now_et()
        except ImportError:
            end_time = self.exit_time or datetime.now()
        entry = self.entry_time
        if isinstance(end_time, str):
            end_time = datetime.fromisoformat(end_time)
        if isinstance(entry, str):
            entry = datetime.fromisoformat(entry)
        # P0 修复 (2026-05-11): 复用 get_market_hours_us 避免 wall-clock 计算
        try:
            from app.risk.position_exit_monitor import get_market_hours_us
            return get_market_hours_us(entry, end_time)
        except Exception:
            return (end_time - entry).total_seconds() / 3600
    
    def is_closed(self) -> bool:
        """持仓是否已平仓"""
        return self.exit_time is not None


@dataclass
class TradeEvent:
    """
    交易事件
    
    统一事件流，支持回放和审计
    """
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    event_type: str = ""
    timestamp: datetime = field(default_factory=datetime.now)
    
    # 关联对象
    signal_id: Optional[str] = None
    order_id: Optional[str] = None
    position_id: Optional[str] = None
    trace_id: Optional[str] = None   # 全链路归因 ID (Phase 2)
    
    # 事件数据
    data: Dict[str, Any] = field(default_factory=dict)
    
    # 元数据
    source: str = ""               # 事件来源 (decision/risk/execution/observability)
    severity: AlertLevel = AlertLevel.INFO
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "timestamp": self.timestamp.isoformat(),
            "signal_id": self.signal_id,
            "order_id": self.order_id,
            "position_id": self.position_id,
            "data": self.data,
            "source": self.source,
            "severity": self.severity.value,
        }


@dataclass
class FailureRecord:
    """
    失败记录
    
    详细的失败分类和归因
    """
    failure_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    failure_type: FailureType = FailureType.SIGNAL_EXPIRED
    
    timestamp: datetime = field(default_factory=datetime.now)
    symbol: str = ""
    
    # 关联对象
    signal_id: Optional[str] = None
    order_id: Optional[str] = None
    position_id: Optional[str] = None
    
    # 失败详情
    reason: str = ""
    details: Dict[str, Any] = field(default_factory=dict)
    
    # 恢复信息
    recovered: bool = False
    recovery_time: Optional[datetime] = None
    recovery_action: str = ""
    
    # 影响评估
    affects_funds: bool = False
    triggers_circuit_breaker: bool = False
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "failure_id": self.failure_id,
            "failure_type": self.failure_type.value,
            "timestamp": self.timestamp.isoformat(),
            "symbol": self.symbol,
            "signal_id": self.signal_id,
            "order_id": self.order_id,
            "position_id": self.position_id,
            "reason": self.reason,
            "details": self.details,
            "recovered": self.recovered,
            "recovery_time": self.recovery_time.isoformat() if self.recovery_time else None,
            "recovery_action": self.recovery_action,
            "affects_funds": self.affects_funds,
            "triggers_circuit_breaker": self.triggers_circuit_breaker,
        }


@dataclass
class Alert:
    """
    告警对象
    """
    alert_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    level: AlertLevel = AlertLevel.INFO
    
    title: str = ""
    message: str = ""
    timestamp: datetime = field(default_factory=datetime.now)
    
    # 关联对象
    signal_id: Optional[str] = None
    order_id: Optional[str] = None
    position_id: Optional[str] = None
    
    # 告警来源
    source: str = ""
    category: str = ""             # risk/execution/system/strategy
    
    # 处理状态
    acknowledged: bool = False
    acknowledged_at: Optional[datetime] = None
    acknowledged_by: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "alert_id": self.alert_id,
            "level": self.level.value,
            "title": self.title,
            "message": self.message,
            "timestamp": self.timestamp.isoformat(),
            "signal_id": self.signal_id,
            "order_id": self.order_id,
            "position_id": self.position_id,
            "source": self.source,
            "category": self.category,
            "acknowledged": self.acknowledged,
        }
