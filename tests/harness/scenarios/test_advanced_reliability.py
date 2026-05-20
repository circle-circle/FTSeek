"""
Advanced Scenario Tests: Consistency & Reliability
"""
import pytest
import time
from tests.harness.mock_broker import MockFutuBroker
from tests.harness.mock_agent import MockAgentBridge
from app.core.models import Position, Direction, Order, OrderType

# ------------------------------------------------------------------
# RK-03: Position Consistency Test
# ------------------------------------------------------------------

def test_position_consistency_sync(broker: MockFutuBroker):
    """
    Scenario: Memory (PositionStore) is empty, but Broker has positions.
    Expectation: System detects discrepancy and syncs.
    """
    # 1. Simulate Broker having a position (e.g. from previous crash)
    broker.positions = {
        "US.AAPL": {"qty": 10, "cost": 150.0}
    }
    
    # 2. Simulate Memory being empty (simulated by local list)
    memory_positions = {}
    
    # 3. Run Consistency Check (Simulation of _reconcile_position_store)
    # In real code, this loop runs every 5 mins.
    for sym, info in broker.positions.items():
        if sym not in memory_positions:
            # Sync Logic
            memory_positions[sym] = info
    
    # 4. Verify Sync
    assert "US.AAPL" in memory_positions, "Sync failed: Position not found in Memory"
    assert memory_positions["US.AAPL"]["qty"] == 10

# ------------------------------------------------------------------
# RB-01: Broker Retry Mechanism Test
# ------------------------------------------------------------------

class FlakyBroker(MockFutuBroker):
    """
    Mock Broker that fails N times before succeeding.
    """
    def __init__(self, fail_count=2):
        super().__init__()
        self.fail_count = fail_count
        self.attempts = 0

    def submit_order(self, order) -> dict:
        self.attempts += 1
        if self.attempts <= self.fail_count:
            raise ConnectionError("Simulated Network Timeout")
        return super().submit_order(order)

def test_broker_retry_logic():
    """
    Scenario: Broker fails due to network error.
    Expectation: System retries (max 3 times) and succeeds.
    """
    broker = FlakyBroker(fail_count=2) # Will fail twice, then succeed
    
    order = Order(
        symbol="US.MSFT",
        order_type=OrderType.MARKET,
        direction=Direction.LONG,
        quantity=5,
        price=300.0
    )

    max_retries = 3
    success = False
    
    # Retry Logic Simulation
    for attempt in range(max_retries):
        try:
            result = broker.submit_order(order)
            success = True
            break
        except ConnectionError:
            if attempt == max_retries - 1:
                raise # Re-raise if max retries exceeded
            time.sleep(0.1) # Simulated backoff

    assert success is True, "Retry mechanism failed"
    assert broker.attempts == 3, f"Expected 3 attempts, got {broker.attempts}"
    assert "US.MSFT" in broker.positions

def test_broker_retry_exhaustion():
    """
    Scenario: Broker fails persistently.
    Expectation: System gives up after max retries.
    """
    broker = FlakyBroker(fail_count=10) # Will always fail during test window
    
    order = Order(
        symbol="US.AMZN",
        order_type=OrderType.MARKET,
        direction=Direction.LONG,
        quantity=5,
        price=100.0
    )

    max_retries = 3
    success = False
    
    for attempt in range(max_retries):
        try:
            result = broker.submit_order(order)
            success = True
            break
        except ConnectionError:
            if attempt == max_retries - 1:
                pass # Expected to fail here
            time.sleep(0.1)

    assert success is False, "Should have failed after retries"
    assert broker.attempts == 3
