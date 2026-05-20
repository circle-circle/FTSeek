"""
Simulated Clock for Deterministic Testing
"""
from datetime import datetime, timedelta

class SimClock:
    def __init__(self, start: datetime):
        self._now = start

    def now(self) -> datetime:
        return self._now

    def tick(self, minutes: int):
        """Advance time by minutes"""
        self._now += timedelta(minutes=minutes)

    def is_market_open(self) -> bool:
        """
        Simplified Market Open Logic for Simulation:
        Mon-Fri, 09:30 - 16:00 ET
        """
        weekday = self._now.weekday()
        hour = self._now.hour
        minute = self._now.minute
        
        if weekday >= 5: # Sat, Sun
            return False
        
        time_mins = hour * 60 + minute
        return (9 * 60 + 30) <= time_mins < (16 * 60)
