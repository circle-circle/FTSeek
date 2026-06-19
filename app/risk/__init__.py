"""风险引擎模块

2026-06-14: 删除 TradingFrequencyLimiter (零生产消费者)
2026-06-17: 新增 AbsoluteCircuitBreaker (绝对风控底线) + SignalDeduplicator (信号去重)
"""
from .circuit_breaker import (
    StrategyCircuitBreaker,
    SymbolCircuitBreaker,
    SystemCircuitBreaker,
)
from .absolute_circuit_breaker import AbsoluteCircuitBreaker, get_absolute_circuit_breaker
from .signal_dedup import SignalDeduplicator, get_signal_deduplicator
from .drawdown_control import DrawdownController
from .position_limits import PositionLimitsChecker
from .sector_limits import SectorLimitsChecker

__all__ = [
    "StrategyCircuitBreaker",
    "SymbolCircuitBreaker",
    "SystemCircuitBreaker",
    "AbsoluteCircuitBreaker",
    "get_absolute_circuit_breaker",
    "SignalDeduplicator",
    "get_signal_deduplicator",
    "DrawdownController",
    "PositionLimitsChecker",
    "SectorLimitsChecker",
]
