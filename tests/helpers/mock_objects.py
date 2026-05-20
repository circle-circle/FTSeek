"""
Mock objects and helpers for FTSeek-US scenario testing.
"""

import time
import uuid
from dataclasses import dataclass
from typing import Optional, List, Dict, Any

# 1. Helper to construct the real 'order_context' dict expected by TradeGuard.evaluate()
def make_order_context(
    symbol: str,
    direction: str,  # OPEN_LONG, OPEN_SHORT, CLOSE
    signal_strength: float = 50,
    is_reversal: bool = False,
    current_position_dir: Optional[str] = None,
    current_holding_strength: float = 0,
    volatility: float = 0.2,
    recent_news: Optional[List[Dict[str, Any]]] = None,
    trace_id: Optional[str] = None,
    **kwargs
) -> Dict[str, Any]:
    """
    Constructs a valid order_context dict for TradeGuard.evaluate().
    """
    return {
        "trace_id": trace_id or str(uuid.uuid4())[:8],
        "symbol": symbol,
        "direction": direction,
        "signal_strength": signal_strength,
        "is_reversal": is_reversal,
        "current_position_dir": current_position_dir,
        "current_holding_strength": current_holding_strength,
        "volatility": volatility,
        "recent_news": recent_news or [],
        **kwargs
    }

def make_news(
    symbol: str,
    title: str,
    source: str = "reuters",
    timestamp: Optional[float] = None,
    **kwargs
) -> Dict[str, Any]:
    """
    Creates a news item dict compatible with TradeGuard.
    """
    return {
        "symbol": symbol,
        "title": title,
        "source": source,
        "timestamp": timestamp or time.time(),
        **kwargs
    }

# 2. Mock TradeGuard Factory
def get_test_guard(equity: float = 100000, data_dir: str = "data", cooldown_minutes: float = 15.0):
    """
    Returns a real TradeGuard instance configured for testing.
    """
    from app.core.trade_guard import TradeGuard, ReverseEngine
    
    config = {
        "data_dir": data_dir,
        "equity": equity,
    }
    
    guard = TradeGuard(config)
    # Allow customizing the reverse engine if needed (though TradeGuard instantiates it internally)
    # We can access guard.reverse_engine to manipulate state
    guard.reverse_engine.cooldown_base = cooldown_minutes
    return guard

# 3. Mock Executor & AuditStore for "Link" testing
class MockExecutor:
    """
    Simulates order execution.
    """
    def __init__(self, force_fail: bool = False):
        self.force_fail = force_fail
        self.calls = []

    def execute(self, signal: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append(signal)
        if self.force_fail:
            return {"success": False, "error": "Simulated Broker Rejection"}
        return {"success": True, "order_id": f"MOCK-{time.time()}"}

class MockAuditStore:
    """
    Simulates audit recording.
    """
    def __init__(self):
        self.records = []

    def write(self, record: Dict[str, Any]):
        self.records.append(record)

    def last(self) -> Optional[Dict[str, Any]]:
        return self.records[-1] if self.records else None
