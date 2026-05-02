"""
Event Bus — 事件总线

目的：模块间通过事件通信，替代直接 import 调用，实现彻底解耦。

使用方式:
    from app.core.event_bus import EventBus, TradeEvent
    
    # 订阅事件
    EventBus.subscribe(TradeEvent.POSITION_CLOSED, notifier.on_position_closed)
    
    # 发布事件（同步）
    EventBus.publish(TradeEvent.POSITION_CLOSED, {
        "code": "HK.00700", "pnl": 1000, "reason": "stop_loss"
    })
    
    # 发布事件（异步，不阻塞调用方）
    EventBus.publish_async(TradeEvent.POSITION_CLOSED, {...})
"""

import asyncio
import logging
import threading
from typing import Callable, Dict, List, Any, Optional
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger("ftseek.eventbus")


@dataclass
class Event:
    """事件对象"""
    name: str
    payload: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)
    source: str = ""


class EventBus:
    """
    事件总线 — 发布/订阅模式
    
    线程安全，支持同步和异步发布。
    事件处理器异常不会阻塞其他处理器。
    """
    
    _handlers: Dict[str, List[Callable]] = {}
    _lock = threading.Lock()
    
    @classmethod
    def subscribe(cls, event_name: str, handler: Callable):
        """
        订阅事件
        
        Args:
            event_name: 事件名称
            handler: 处理函数，签名: handler(event: Event) -> None
        """
        with cls._lock:
            if event_name not in cls._handlers:
                cls._handlers[event_name] = []
            cls._handlers[event_name].append(handler)
            logger.debug(f"订阅事件: {event_name} -> {handler.__module__}.{handler.__name__}")
    
    @classmethod
    def unsubscribe(cls, event_name: str, handler: Callable):
        """取消订阅"""
        with cls._lock:
            if event_name in cls._handlers:
                cls._handlers[event_name] = [
                    h for h in cls._handlers[event_name] if h != handler
                ]
    
    @classmethod
    def publish(cls, event_name: str, payload: Dict[str, Any] = None,
                source: str = "") -> List[Exception]:
        """
        同步发布事件
        
        Returns:
            处理器异常列表（空的表示全部成功）
        """
        event = Event(name=event_name, payload=payload or {}, source=source)
        errors = []
        
        with cls._lock:
            handlers = list(cls._handlers.get(event_name, []))
        
        if not handlers:
            logger.debug(f"事件无订阅者: {event_name}")
            return errors
        
        logger.info(f"发布事件: {event_name} (订阅者={len(handlers)})")
        
        for handler in handlers:
            try:
                handler(event)
            except Exception as e:
                logger.error(f"事件处理器异常 [{event_name}]: {e}", exc_info=True)
                errors.append(e)
        
        return errors
    
    @classmethod
    def publish_async(cls, event_name: str, payload: Dict[str, Any] = None,
                      source: str = ""):
        """
        异步发布事件（不阻塞调用方）
        
        在后台线程中执行所有处理器。
        """
        def _dispatch():
            cls.publish(event_name, payload, source)
        
        thread = threading.Thread(
            target=_dispatch,
            name=f"event-{event_name}",
            daemon=True
        )
        thread.start()
    
    @classmethod
    def get_handlers(cls, event_name: str) -> List[str]:
        """获取事件的处理器列表（用于调试）"""
        with cls._lock:
            handlers = cls._handlers.get(event_name, [])
            return [f"{h.__module__}.{h.__name__}" for h in handlers]
    
    @classmethod
    def clear(cls):
        """清空所有订阅（用于测试）"""
        with cls._lock:
            cls._handlers.clear()


# ===== 预定义事件常量 =====


class MarketEvent:
    """Market data events"""
    QUOTE_RECEIVED = "market.quote_received"
    KLINE_RECEIVED = "market.kline_received"
    KLINE_PROCESSED = "market.kline_processed"
    TREND_UPDATED = "market.trend_updated"
    SUBSCRIPTION_CHANGED = "market.subscription_changed"


class StrategyEvent:
    """Strategy events"""
    SIGNAL_GENERATED = "strategy.signal_generated"
    SIGNAL_REJECTED = "strategy.signal_rejected"
    FACTOR_EVALUATED = "strategy.factor_evaluated"
    STRATEGY_HEALTH = "strategy.health_check"


class RiskEvent:
    """Risk management events"""
    STOP_LOSS_TRIGGERED = "risk.stop_loss_triggered"
    TAKE_PROFIT_TRIGGERED = "risk.take_profit_triggered"
    TRAILING_STOP_UPDATED = "risk.trailing_stop_updated"
    POSITION_LIMIT_WARNING = "risk.position_limit_warning"
    CIRCUIT_BREAKER_TRIGGERED = "risk.circuit_breaker_triggered"


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


class SystemEvent:
    """系统事件"""
    SERVICE_STARTED = "service.started"
    SERVICE_STOPPED = "service.stopped"
    HEALTH_CHECK = "health.check"
    CONFIG_RELOADED = "config.reloaded"
    MODE_SWITCHED = "mode.switched"
