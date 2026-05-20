"""
Global Test Fixtures
"""
import pytest
import os
import sys
from datetime import datetime

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from tests.harness.sim_clock import SimClock
from tests.harness.mock_broker import MockFutuBroker
from tests.harness.mock_agent import MockAgentBridge

@pytest.fixture
def clock():
    # Start at Monday 09:35 ET (Intra-day)
    return SimClock(datetime(2026, 5, 4, 9, 35))

@pytest.fixture
def broker():
    return MockFutuBroker()

@pytest.fixture
def agent():
    return MockAgentBridge()
