"""
FTSeek-US Scalping / Mean-Reversion Engine

Pure statistical triggers (RSI, VWAP, Bollinger Bands).
No LLM, no prediction — only price deviation → reversion.
Thread-safe, budget-isolated, time-gated.
"""

from app.scalping.engine import ScalpingEngine
from app.scalping.time_gate import TimeGate
from app.scalping.signals import SignalDetector
from app.scalping.exits import FixedExit, TimeExit, AdaptiveExit
from app.scalping.budget import ScalpingBudget
from app.scalping.circuit_breaker import ScalpingCircuitBreaker
from app.scalping.trend_filter import TrendFilter
from app.scalping.stats import ScalpingStatsTracker

__all__ = [
    "ScalpingEngine",
    "TimeGate",
    "SignalDetector",
    "FixedExit",
    "TimeExit",
    "AdaptiveExit",
    "ScalpingBudget",
    "ScalpingCircuitBreaker",
    "TrendFilter",
    "ScalpingStatsTracker",
]
