"""
shared package — Cross-market shared utilities for FTSeek dual-market system.

These modules are designed to be importable from both FTSeek (HK) and FTSeek-US.
US access: symlink FTSeek-US/app/shared → ../FTSeek/app/shared
"""

from .market_hours import (
    MarketType,
    MarketPhase,
    get_market_phase,
    is_trading_hours,
    is_trading_day,
    get_session_config,
    get_current_time,
    get_trading_periods,
    MarketHoursCompat,
    MarketHours,  # Alias for MarketHoursCompat (backward compat)
    MarketHoursValidator,
)

__all__ = [
    "MarketType",
    "MarketPhase",
    "get_market_phase",
    "is_trading_hours",
    "is_trading_day",
    "get_session_config",
    "get_current_time",
    "get_trading_periods",
    "MarketHoursCompat",
    "MarketHours",  # Alias for MarketHoursCompat
    "MarketHoursValidator",
]
