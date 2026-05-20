"""
Data Transfer Objects (DTOs) for FTSeek-US

Replaces loose Dict usage with strict type-hinted dataclasses.
Ensures data integrity between pipeline layers (Signal -> Order -> Execution).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional
from datetime import datetime


@dataclass
class OrderRequest:
    """
    Request object for executing a trade order.
    Replaces the 'context' dict passed to _submit_order.
    """
    symbol: str
    direction: str  # "LONG" or "SHORT"
    
    # Signal Info
    signal_strength: int = 0
    signal_confidence: float = 0.0
    voting_result: Optional[Dict[str, Any]] = None
    strategy_id: str = ""
    trace_id: str = ""
    
    # Market Context
    price: float = 0.0
    vix_regime: str = "normal"
    market_phase: str = ""
    
    # Execution Params
    position_pct: float = 0.10  # Suggested position % of equity
    vix_position_multiplier: float = 1.0
    vix_stop_loss_adjustment: float = 1.0
    is_add_position: bool = False
    
    # Metadata
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class OrderResult:
    """
    Result object returned by order execution.
    """
    success: bool = False
    order_id: Optional[str] = None
    futu_order_id: Optional[str] = None
    price: float = 0.0
    quantity: float = 0.0
    message: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
