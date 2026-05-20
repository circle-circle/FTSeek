"""
Edge Cases Tests
- Weekend Silence
- Stale Data
- Config Validation
"""
import pytest
from datetime import datetime
from tests.harness.sim_clock import SimClock

def test_weekend_silence():
    clock = SimClock(datetime(2026, 5, 9, 10, 0)) # Saturday
    assert not clock.is_market_open()

def test_weekday_outside_hours():
    clock = SimClock(datetime(2026, 5, 4, 8, 00)) # Monday Pre-market
    assert not clock.is_market_open()

def test_weekday_market_hours():
    clock = SimClock(datetime(2026, 5, 4, 10, 00)) # Monday Market
    assert clock.is_market_open()

def test_time_tick():
    clock = SimClock(datetime(2026, 5, 4, 9, 30))
    assert clock.is_market_open()
    
    clock.tick(400) # + 6h 40m = 16:10
    assert not clock.is_market_open()

def test_stale_data_detection():
    import time
    # Simulate stale data check
    last_update = time.time() - 3600 # 1 hour ago
    threshold = 60 # 1 min
    
    is_stale = (time.time() - last_update) > threshold
    assert is_stale
