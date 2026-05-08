You are conducting a dialectical debate (PRO vs ANTI) about creating a shared `market_hours.py` module for FTSeek (dual-market trading system: 港股 HK + 美股 US).

## Context: ACTUAL codebase state (verified by reading files)

### FTSeek-HK — Time/session logic locations:

**1. app/utils/session_detector.py** (85 lines)
```python
class SessionDetector:
    def __init__(self, time_aware_config: Dict):
        # Reads from config_center, supports: opening, mid_day, afternoon, closing
        # Uses datetime.now().time() — CST, no timezone awareness
        priority_order = ['opening', 'mid_day', 'afternoon', 'closing']
    
    def get_current_session(self) -> MarketSession:
        # Returns config dict for current time
```

**2. app/utils/holiday_calendar.py** (173 lines)
```python
class TradingHolidayCalendar:  # Singleton
    HK_HOLIDAYS_2026 = {date(2026,1,1), ...}  # 15 days
    US_HOLIDAYS_2026 = {date(2026,1,1), ...}  # 11 days
    is_trading_day(market, date) -> bool  # checks weekday + holiday
```

**3. app/data_subscriber.py:475** — DataSubscriber._is_trading_hours()
```python
# DUPLICATED hardcoded logic, NOT using session_detector or holiday_calendar
# Hardcoded HK times: 09:30-12:00, 13:00-16:00 (CST implicit)
if weekday >= 5: return False
if morning_start <= current_time < morning_end: return True
```

**4. app/strategy/market_regime.py** (407 lines)
- ADX/ATR based regime detection — NOT time-based
- Should NOT merge into market_hours

### FTSeek-US — Time/session logic:

**5. app/core/scheduler_triggers.py** (~500 lines)
```python
ET = pytz.timezone("America/New_York")

class MarketPhase(Enum):
    TRADING = "trading"           # 09:30-16:00 ET, weekdays
    PRE_MARKET = "pre_market"     # 04:00-09:30 ET
    AFTER_HOURS = "after_hours"   # 16:00-20:00 ET
    OVERNIGHT = "overnight"       # 20:00-04:00 ET
    WEEKEND = "weekend"

def get_market_phase() -> MarketPhase:  # Uses ET timezone
def is_market_open() -> bool  # Wrapper for TRADING check
# Also contains: check_daily_review, check_weekly_review, check_monthly_review
# check_position_snapshot — tightly coupled with scheduler
```

### Key architectural differences:
| Aspect | HK | US |
|--------|----|-----|
| Timezone | CST implicit (no pytz) | pytz America/New_York |
| Phase granularity | 5 (opening/mid_day/afternoon/closing/default) | 5 (TRADING/PRE_MARKET/AFTER_HOURS/OVERNIGHT/WEEKEND) |
| Holiday check | holiday_calendar.py (unified for both) | holiday_calendar.py (same file) |
| Data source | Futu SDK (local WebSocket) | REST APIs (Finnhub, etc.) |
| Lunch break | YES (12:00-13:00) | NO |
| Pre-market | 09:00-09:30 (竞价) | 04:00-09:30 (extended) |
| After-hours | 16:00-16:10 (CAS) | 16:00-20:00 (extended) |

### Proposed shared module design:
```
shared/market_hours.py
├── MarketType(Enum): HK, US
├── MarketPhase(Enum): unified phase names
├── MarketHoursConfig: config-driven session times
├── is_trading_day(market, date) -> bool
├── is_weekend(date) -> bool
├── get_market_phase(market, dt) -> MarketPhase
├── is_trading_hours(market, dt) -> bool
├── get_trading_periods(market) -> List[Dict]
└── wait_for_next_session(market) -> void
```

## Debate: 5 Rounds of PRO vs ANTI

### Round 1: Should we create a SHARED module or keep SEPARATE?
Debate the core question: one unified module vs separate HK/US modules.

### Round 2: Timezone — CONFIG-DRIVEN or CODE-DRIVEN?
Should timezone be read from config, or hardcoded in the module?

### Round 3: Module BOUNDARY — Broad or Narrow?
Should it include holiday calendar + review triggers, or ONLY time-to-phase mapping?

### Round 4: Migration RISK — What breaks?
Analyze blast radius, shadow mode feasibility, rollback strategy.

### Round 5: Impact on Fincept News + AkShare integration
Does market_hours need to know about news processing schedules?

## Deliverables:
1. VERDICT per round with reasoning
2. Recommended file structure (exact paths)
3. Migration plan with ordered steps
4. Exact public API (function signatures)
5. Top 5 anti-patterns based on actual code analysis
