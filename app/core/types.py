"""Shared types for FTSeek - Decoupled module for cross-layer data structures.

This module contains lightweight data types used across strategy, trader, and intraday layers
to prevent tight coupling. Strategy implementations should import from here instead of
app.strategy.base for Signal/SignalAction.
"""
from __future__ import annotations
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Dict, Any


class SignalAction(str, Enum):
    """Signal action enum"""
    BUY = "BUY"
    SELL = "SELL"
    ALERT = "ALERT"


class TradeTier(str, Enum):
    """Trade tier enum"""
    TIER_TREND = "TREND"
    TIER_SHORT = "SHORT_ALPHA"
    TIER_SCALP = "SCALP"


class SignalSource(str, Enum):
    """Signal source enum"""
    TECHNICAL = "TECHNICAL"
    FUNDAMENTAL = "FUNDAMENTAL"
    SENTIMENT = "SENTIMENT"
    LLM = "LLM"
    EVENT = "EVENT"


@dataclass
class Signal:
    """A trading signal (lightweight shared data structure)"""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    code: str = ""
    name: str = ""
    action: SignalAction = SignalAction.ALERT
    price: float = 0.0
    quantity: int = 1
    source: SignalSource = SignalSource.TECHNICAL
    reason: str = ""
    strategy: str = ""
    trace_id: str = ""
    priority: int = 2
    timestamp: float = field(default_factory=time.time)
    expire_at: float = 0.0
    confirmed: bool = False
    executed: bool = False
    tier: TradeTier = TradeTier.TIER_TREND
    tier_stop_loss: float = 0.0
    extra: Optional[Dict[str, Any]] = None
