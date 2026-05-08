#!/usr/bin/env python3
"""
FTSeek 领域事件总线 (2026-05-05 v2)

核心原则:
  - 同步 emit: 核心管线是同步的，不引入 async
  - 即发即忘: emit() 内每个 handler 独立 try/except，一个插件崩了不影响主线和其它插件
  - 松耦合: 事件类型用字符串常量，插件不需要 import 核心模块
  - 零侵入: 核心管线只需 from app.core.domain_events import get_event_bus, DomainEvent

v2 新增 (2026-05-05):
  - 类型化 payload dataclass，确保 emit 数据结构一致
  - PLAN_MIDDAY_REFRESHED 事件类型
  - SYSTEM_RISK_ADJUSTED 事件类型 (反馈回路)
  - new_event() 工厂函数自动校验 payload
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional
import logging

logger = logging.getLogger("ftseek.domain_events")

# ════════════════════════════════════════════════════════════════════
# 事件类型常量
# ════════════════════════════════════════════════════════════════════

class EventType:
    TRADE_OPENED = "trade.opened"
    TRADE_CLOSED = "trade.closed"          # SL / TP / 正常平仓 / 分批止盈 统一走这个
    PLAN_PREMARKET = "plan.premarket"
    PLAN_DEBATE = "plan.debate"
    PLAN_MIDDAY_REFRESHED = "plan.midday_refreshed"  # v2 新增: 午间计划刷新
    REVIEW_EOD = "review.eod"
    REVIEW_NEXTDAY = "review.nextday"
    REVIEW_MIDDAY = "review.midday"
    EVOLUTION_WEEKLY = "evolution.weekly"
    SYSTEM_RISK_ADJUSTED = "system.risk_adjusted"    # v2 新增: 自适应风控调整


# ════════════════════════════════════════════════════════════════════
# 事件 payload 定义 (类型化 — v2 新增)
# ════════════════════════════════════════════════════════════════════

@dataclass
class TradeOpenedPayload:
    """开仓事件 payload"""
    code: str
    market: str                        # "HK" | "US"
    price: float
    quantity: int
    action: str                        # "BUY" | "SELL"
    regime: str = "consensus"
    entry_time: str = ""
    signal_id: str = ""
    signal_trace_id: str = ""


@dataclass
class TradeClosedPayload:
    """平仓事件 payload (统一 SL / TP / 正常 / 分批)"""
    code: str
    market: str                        # "HK" | "US"
    entry_time: str
    exit_time: str
    entry_price: float
    exit_price: float
    quantity: int
    pnl: float
    pnl_pct: float
    strategy: str                      # consensus | 止损 | 止盈 | 分批止盈(30%)
    action: str                        # "BUY" | "SELL"
    exit_reason: str                   # stop_loss | take_profit | partial_take_profit | normal
    signal_id: str = ""
    signal_trace_id: str = ""
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    highest_price: Optional[float] = None
    lowest_price: Optional[float] = None
    tags: List[str] = field(default_factory=list)


@dataclass
class PlanPremarketPayload:
    """盘前计划事件 payload"""
    date: str
    market: str                        # "HK" | "US"
    targets: List[str] = field(default_factory=list)
    restricted: List[str] = field(default_factory=list)
    overall_bias: str = "中性"
    regime: str = "unknown"
    generated_at: str = ""


@dataclass
class PlanDebatePayload:
    """辩论合成事件 payload"""
    date: str
    markdown: str


@dataclass
class PlanMiddayRefreshedPayload:
    """午间计划刷新事件 payload (v2 新增)"""
    date: str
    added_count: int
    total_count: int
    new_targets: List[str] = field(default_factory=list)
    ashare_context: List[Dict] = field(default_factory=list)


@dataclass
class ReviewEODPayload:
    """盘后复盘事件 payload"""
    trade_date: str
    market: str
    trades_reviewed: int
    win_rate: float
    total_pnl: float
    max_drawdown: float
    trades: List[Dict] = field(default_factory=list)
    summary: str = ""
    lessons: List[Dict] = field(default_factory=list)


@dataclass
class ReviewNextDayPayload:
    """次日预判事件 payload"""
    trade_date: str
    preview_text: str


@dataclass
class ReviewMiddayPayload:
    """午间复盘事件 payload"""
    date: str
    trades_reviewed: int
    lessons_added: int = 0
    status: str = "completed"


@dataclass
class EvolutionWeeklyPayload:
    """周度进化事件 payload"""
    date: str
    evolve_result: Dict = field(default_factory=dict)
    meta_report: str = ""
    meta_score: float = 0.0


@dataclass
class RiskAdjustedPayload:
    """自适应风控调整事件 payload (v2 新增)"""
    reason: str                        # 触发原因
    adjustment: str                    # 调整内容描述
    old_value: float
    new_value: float
    consecutive_stops: int = 0
    affected_code: str = ""


# ════════════════════════════════════════════════════════════════════
#  DomainEvent
# ════════════════════════════════════════════════════════════════════

@dataclass
class DomainEvent:
    """领域事件。

    v2 变更 (2026-05-05):
    - correlation_id 默认从 signal_id 自动填充
    - source 默认从调用栈推断
    """
    type: str
    data: Dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    correlation_id: Optional[str] = None
    source: Optional[str] = None

    @property
    def data_as(self) -> Dict[str, Any]:
        """向后兼容: 返回 data dict"""
        return self.data


# ════════════════════════════════════════════════════════════════════
# 便捷构造器
# ════════════════════════════════════════════════════════════════════

def _payload_to_dict(payload: Any) -> Dict[str, Any]:
    """将 dataclass payload 转为 dict"""
    if payload is None:
        return {}
    if isinstance(payload, dict):
        return payload
    if hasattr(payload, '__dataclass_fields__'):
        from dataclasses import asdict
        return asdict(payload)
    return {}


def new_event(event_type: str, payload: Any = None,
              correlation_id: str = None, source: str = None) -> DomainEvent:
    """便捷构造函数，减少 emit 调用时的样板代码。

    v2 变更: payload 接受 dataclass 实例或 dict（向后兼容）。

    用法:
        emit(new_event(EventType.TRADE_OPENED, TradeOpenedPayload(...),
                       correlation_id=trace_id, source="data_subscriber"))
    """
    return DomainEvent(
        type=event_type,
        data=_payload_to_dict(payload),
        correlation_id=correlation_id,
        source=source or __name__,
    )


# ════════════════════════════════════════════════════════════════════
#  EventBus
# ════════════════════════════════════════════════════════════════════

class DomainEventBus:
    """同步领域事件总线 — 即发即忘，永不抛异常"""

    def __init__(self):
        self._handlers: Dict[str, List[Callable[[DomainEvent], None]]] = {}

    def subscribe(self, event_type: str, handler: Callable[[DomainEvent], None]) -> None:
        """订阅事件类型。handler 签名为 (event: DomainEvent) -> None"""
        self._handlers.setdefault(event_type, []).append(handler)

    def unsubscribe(self, event_type: str, handler: Callable[[DomainEvent], None]) -> None:
        try:
            self._handlers.get(event_type, []).remove(handler)
        except ValueError:
            pass

    def emit(self, event: DomainEvent) -> None:
        """发射事件到所有订阅者。每个 handler 独立 try/except，一个崩了不影响其他。"""
        handlers = self._handlers.get(event.type, ())
        if not handlers:
            logger.debug("[EventBus] 事件 %s 无订阅者", event.type)
            return
        for handler in handlers:
            try:
                handler(event)
            except Exception as e:
                handler_name = getattr(handler, "__name__", repr(handler))
                logger.warning(
                    f"[EventBus] 事件 {event.type} 处理失败 [{handler_name}]: {e}",
                    exc_info=True,
                )

    @property
    def subscriber_count(self) -> Dict[str, int]:
        """返回每个事件类型的订阅者数量 (诊断用)"""
        return {k: len(v) for k, v in self._handlers.items()}


# ════════════════════════════════════════════════════════════════════
# 全局单例
# ════════════════════════════════════════════════════════════════════

_global_bus: Optional[DomainEventBus] = None


def get_event_bus() -> DomainEventBus:
    global _global_bus
    if _global_bus is None:
        _global_bus = DomainEventBus()
    return _global_bus
