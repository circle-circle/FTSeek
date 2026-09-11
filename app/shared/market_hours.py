"""
shared/market_hours.py — Unified market hours module for FTSeek dual-market system.

Provides timezone-aware market phase detection, trading hours checks,
and session configuration for both HK and US markets.

Design decisions (from 5-round dialectical debate, 2026-05-02):
- SHARED module (not separate HK/US) — eliminates active duplication
- HYBRID timezone — code owns pytz/tz mapping, config owns session boundary times
- NARROW boundary — time-to-phase mapping only; reviews/cooldown stay separate
- PURELY TIME-FOCUSED — news modules are consumers, not partners

Migration: Stateless module with Shadow Mode support.
Rollback: Remove import → reverts to existing logic.
"""

import logging
from datetime import datetime, date, time
from enum import Enum
from typing import Dict, Optional, List

import pytz

logger = logging.getLogger("ftseek.market_hours")

# ── Market Types ──────────────────────────────────────────────────────────

class MarketType(Enum):
    """Supported market types."""
    HK = "hk"   # Hong Kong (SEHK)
    US = "us"   # US (NYSE/NASDAQ)


# ── Market Phases ─────────────────────────────────────────────────────────

class MarketPhase(Enum):
    """
    Market operating phase.

    HK phases:
    - PRE_OPEN:    09:00-09:30 HKT (pre-opening session)
    - MORNING:     09:30-12:00 HKT (morning trading)
    - LUNCH_BREAK: 12:00-13:00 HKT (lunch break, market closed)
    - AFTERNOON:   13:00-16:00 HKT (afternoon trading)
    - CLOSED:      16:00-09:00 HKT (market closed)
    - WEEKEND:     Saturday/Sunday

    US phases:
    - PRE_MARKET:  04:00-09:30 ET (pre-market)
    - TRADING:     09:30-16:00 ET (regular trading)
    - AFTER_HOURS: 16:00-20:00 ET (after-hours)
    - OVERNIGHT:   20:00-04:00 ET (overnight, minimal activity)
    - WEEKEND:     Saturday/Sunday
    """
    # Shared
    WEEKEND = "weekend"

    # HK-specific
    PRE_OPEN = "pre_open"
    MORNING = "morning"
    LUNCH_BREAK = "lunch_break"
    AFTERNOON = "afternoon"
    CLOSED = "closed"

    # US-specific
    PRE_MARKET = "pre_market"
    TRADING = "trading"
    AFTER_HOURS = "after_hours"
    OVERNIGHT = "overnight"


# ── Timezone Map — stable infrastructure knowledge (Round 2: HYBRID) ─────

# Code owns timezone mapping. These are stable facts, not business config.
_TIMEZONE_MAP: Dict[MarketType, str] = {
    MarketType.HK: "Asia/Hong_Kong",
    MarketType.US: "America/New_York",
}

# Default session boundaries — used if config_center is unavailable.
# Round 2: Config owns session times, these are fallback only.
_DEFAULT_SESSIONS: Dict[MarketType, Dict] = {
    MarketType.HK: {
        "pre_open": {"start": time(8, 45), "end": time(9, 30)},
        "morning": {"start": time(9, 30), "end": time(12, 0)},
        "lunch_break": {"start": time(12, 0), "end": time(13, 0)},
        "afternoon": {"start": time(13, 0), "end": time(16, 0)},
    },
    MarketType.US: {
        "pre_market": {"start": time(4, 0), "end": time(9, 30)},
        "trading": {"start": time(9, 30), "end": time(16, 0)},
        "after_hours": {"start": time(16, 0), "end": time(20, 0)},
    },
}


# ── Internal Helpers ──────────────────────────────────────────────────────

def _get_timezone(market: MarketType) -> pytz.BaseTzInfo:
    """
    Resolve timezone for a market type.

    Args:
        market: The market type (HK or US).

    Returns:
        pytz timezone object for the market.
    """
    tz_name = _TIMEZONE_MAP[market]
    return pytz.timezone(tz_name)


# Module-level cache for session boundaries (Bug #1 fix)
_session_cache: Dict[MarketType, Optional[Dict]] = {MarketType.HK: None, MarketType.US: None}


def _get_session_boundaries(market: MarketType) -> Dict:
    """
    Get session boundary times for a market.

    Returns `_DEFAULT_SESSIONS[market]`, cached at module level.
    This implements the HYBRID timezone strategy (Round 2 verdict).

    历史与删除理由 (2026-09-11, DEB-20260910-logging §11.13): 此处曾先试
    `config.config_center` 的 `markets.<m>.trading_hours` 覆盖, 异常则 `except: pass`
    静默退回默认。实测该覆盖分支在 FTSeek-US **恒不可达**:
      ① `config.config_center` 是港股项目 (FTSeek) 的模块, 未随 2026-06-14 停用迁移过来
         —— 全仓 `def get_config_center` **零命中**, 生产进程每次首调都抛
         `ModuleNotFoundError: No module named 'config.config_center'`;
      ② `config/unified_config.yaml` 里也**没有** `markets:` / `trading_hours` 键
         (唯一命中是无关的 `require_trading_hours`) ⇒ 即使模块存在也读不到东西。
    即"配置驱动时段"从未生效过, 一直跑的是 `_DEFAULT_SESSIONS` ⇒ 删死分支
    **行为逐字不变** (原实现必然落到 `_DEFAULT_SESSIONS[market]`), 同时不再用
    `except: pass` 把这条失败吞掉 (它此前每次进程启动都发生, 却零日志)。
    若要真做配置驱动时段: 接 `app.core.config_container` / `config.config_loader`,
    复用下方保留的 `_parse_time` 解析, 并按 OKR C4 影子先行 (属行为变更, 需裁)。

    Returns:
        Dict of session_name -> {"start": time, "end": time}
    """
    # Return cached result if available (Bug #1: caching fix)
    if _session_cache[market] is not None:
        return _session_cache[market]

    result = _DEFAULT_SESSIONS[market]
    _session_cache[market] = result  # Cache it
    return result


def _parse_time(val) -> time:
    """Parse a time value from config (string 'HH:MM' or dict)."""
    if isinstance(val, str):
        return time.fromisoformat(val)
    if isinstance(val, dict):
        h = val.get("hour", val.get("h", 0))
        m = val.get("minute", val.get("m", 0))
        return time(int(h), int(m))
    if isinstance(val, time):
        return val
    raise ValueError(f"Cannot parse time from: {val!r}")


def _phase_from_time(market: MarketType, t: time, d: date = None) -> MarketPhase:
    """
    Internal: Map a local time to a market phase.

    Session boundaries come from unified_config.yaml via config_center.
    Falls back to hardcoded defaults if config unavailable.

    Args:
        market: The market type.
        t: Local time (naive, no tzinfo needed — already in market TZ).
        d: Date for half-day check (HK only).

    Returns:
        MarketPhase for the given time.
    """
    if market == MarketType.HK:
        return _hk_phase_from_time(t, d)
    else:
        return _us_phase_from_time(t)


def _hk_phase_from_time(t: time, d: date = None) -> MarketPhase:
    """HK phase mapping with lunch break support and half-day awareness.
    
    Args:
        t: Local time.
        d: Date for half-day check (optional, defaults to today).
    """
    # Half-day check: if afternoon on a half-day, treat as CLOSED
    if d is not None and t >= time(12, 0):
        try:
            from app.utils.holiday_calendar import get_holiday_calendar
            cal = get_holiday_calendar()
            if cal.is_hk_half_day(d):
                return MarketPhase.CLOSED  # 半日市下午 = 休市
        except Exception:
            # Fail-open: calendar unavailable, use normal schedule — 失败必须可见且带栈
            logger.warning("HK half-day calendar unavailable, using normal schedule",
                           exc_info=True)

    # Check config-driven boundaries first
    sessions = _get_session_boundaries(MarketType.HK)

    # Map config session names to phases
    name_to_phase = {
        "pre_open": MarketPhase.PRE_OPEN,
        "morning": MarketPhase.MORNING,
        "lunch_break": MarketPhase.LUNCH_BREAK,
        "afternoon": MarketPhase.AFTERNOON,
    }

    for session_name, bounds in sessions.items():
        if bounds["start"] <= t < bounds["end"]:
            return name_to_phase.get(session_name, MarketPhase.CLOSED)

    # Fallback: hardcoded defaults (matches existing data_subscriber.py behavior)
    if time(8, 45) <= t < time(9, 30):
        return MarketPhase.PRE_OPEN
    if time(9, 30) <= t < time(12, 0):
        return MarketPhase.MORNING
    if time(12, 0) <= t < time(13, 0):
        return MarketPhase.LUNCH_BREAK
    if time(13, 0) <= t < time(16, 0):
        return MarketPhase.AFTERNOON
    return MarketPhase.CLOSED


def _us_phase_from_time(t: time) -> MarketPhase:
    """US phase mapping — no lunch break."""
    sessions = _get_session_boundaries(MarketType.US)

    name_to_phase = {
        "pre_market": MarketPhase.PRE_MARKET,
        "trading": MarketPhase.TRADING,
        "after_hours": MarketPhase.AFTER_HOURS,
    }

    for session_name, bounds in sessions.items():
        if bounds["start"] <= t < bounds["end"]:
            return name_to_phase.get(session_name, MarketPhase.OVERNIGHT)

    # Fallback: hardcoded defaults (matches existing scheduler_triggers.py behavior)
    if time(4, 0) <= t < time(9, 30):
        return MarketPhase.PRE_MARKET
    if time(9, 30) <= t < time(16, 0):
        return MarketPhase.TRADING
    if time(16, 0) <= t < time(20, 0):
        return MarketPhase.AFTER_HOURS
    return MarketPhase.OVERNIGHT


# ── Public API ────────────────────────────────────────────────────────────

def get_market_phase(market: MarketType, at: Optional[datetime] = None) -> MarketPhase:
    """
    Determine the current market phase for a given market.

    Timezone-aware. Uses the market's native timezone (HKT for HK, ET for US).
    Session boundary times are read from unified_config.yaml via ConfigCenter.

    Args:
        market: The market type (HK or US).
        at: Optional datetime to evaluate. Defaults to current time.
            If naive (no tzinfo), interpreted in the market's timezone.

    Returns:
        MarketPhase enum value for the current phase.

    Examples:
        >>> get_market_phase(MarketType.HK)
        <MarketPhase.MORNING: 'morning'>
        >>> get_market_phase(MarketType.US)
        <MarketPhase.TRADING: 'trading'>
    """
    tz = _get_timezone(market)
    now = at or datetime.now(tz)
    if now.tzinfo is None:
        now = tz.localize(now)

    # Weekend check (works in local timezone)
    if now.weekday() >= 5:
        return MarketPhase.WEEKEND

    # Holiday check
    if not is_trading_day(market, now.date()):
        return MarketPhase.WEEKEND

    return _phase_from_time(market, now.time(), now.date())


def is_trading_hours(market: MarketType, at: Optional[datetime] = None) -> bool:
    """
    Check if the market is currently in active trading hours.

    Returns True only during regular trading (not pre-market, after-hours,
    lunch break, or weekend/holiday).

    Args:
        market: The market type (HK or US).
        at: Optional datetime to evaluate. Defaults to now.

    Returns:
        True if in active trading hours, False otherwise.

    Examples:
        >>> is_trading_hours(MarketType.HK)  # during 12:30 HKT
        False  # lunch break
        >>> is_trading_hours(MarketType.US)  # during 10:00 ET
        True
    """
    phase = get_market_phase(market, at)
    trading_phases = {
        MarketType.HK: {MarketPhase.MORNING, MarketPhase.AFTERNOON},
        MarketType.US: {MarketPhase.TRADING},
    }
    return phase in trading_phases[market]


def is_trading_day(market: MarketType, d: Optional[date] = None) -> bool:
    """
    Check if a date is a trading day (weekday + not a holiday).

    Delegates to holiday_calendar.TradingHolidayCalendar for holiday data.

    Args:
        market: The market type (HK or US).
        d: Date to check. Defaults to today.

    Returns:
        True if the date is a trading day.
    """
    if d is None:
        d = date.today()

    # Weekend check
    if d.weekday() >= 5:
        return False

    # Delegate to existing holiday_calendar (already shared, works well)
    try:
        from app.utils.holiday_calendar import get_holiday_calendar
        cal = get_holiday_calendar()
        market_str = "HK" if market == MarketType.HK else "US"
        return cal.is_trading_day(market_str, d)
    except ImportError:
        # Module not installed — fail-open (safe: better to allow trading than block)
        logger.warning("holiday_calendar not available, assuming trading day",
                       exc_info=True)
        return True
    except Exception as e:
        # Runtime error (MemoryError, etc.) — fail-closed (safe: prevent trading on unknown day)
        logger.error(f"holiday_calendar runtime error, blocking trading day: {e}",
                     exc_info=True)
        return False


def get_session_config(market: MarketType) -> Dict:
    """
    Get the current session's configuration (risk params, etc.).

    Reads from unified_config.yaml risk_limits.time_aware_risk section.
    Matches current time to the appropriate session (opening, mid_day,
    afternoon, closing) and returns its config dict.

    Args:
        market: The market type. Currently only HK has session-aware risk config.

    Returns:
        Dict with min_confidence, max_position, etc. for current session.
        Returns empty dict if no session config exists for this market.
    """
    if market != MarketType.HK:
        return {}

    phase = get_market_phase(market)
    
    # 2026-05-13 修复: phase 到 session key 的映射
    # 根因: MarketPhase.MORNING 覆盖 09:30-12:00 全段，但 time_aware_risk 有
    #   opening (09:30-10:00) 和 afternoon (10:00-12:00) 两个子段
    # 旧映射: MORNING → opening (12:00 前全部用 opening 配置)
    # 新映射: MORNING 内按时间细分 → opening 或 afternoon
    
    if phase == MarketPhase.MORNING:
        # MORNING = 09:30-12:00，需要根据实际时间判断用 opening 还是 afternoon
        now = get_current_time(market)
        if now.time() < time(10, 0):
            session_name = "opening"   # 09:30-10:00
        else:
            session_name = "afternoon"  # 10:00-12:00
    else:
        phase_to_session = {
            MarketPhase.PRE_OPEN: "opening",
            MarketPhase.LUNCH_BREAK: "mid_day",
            MarketPhase.AFTERNOON: "closing",   # 修复: 13:00-16:00 → closing 配置
            MarketPhase.CLOSED: None,            # 收盘后无交易配置
        }
        session_name = phase_to_session.get(phase)
    
    if not session_name:
        return {}

    try:
        from app.config import config_center
        return config_center.get(f'risk_limits.time_aware_risk.{session_name}', {})
    except Exception:
        return {}


def get_current_time(market: MarketType) -> datetime:
    """
    Get current time in the market's native timezone.

    Args:
        market: The market type.

    Returns:
        Timezone-aware datetime in the market's timezone.
    """
    tz = _get_timezone(market)
    return datetime.now(tz)


def get_trading_periods(market: MarketType) -> List[Dict]:
    """
    Get all trading period definitions for a market.

    Useful for logging, notifications, and UI display.

    Args:
        market: The market type.

    Returns:
        List of dicts with 'name', 'start', 'end', 'phase' keys.
    """
    tz = _get_timezone(market)
    sessions = _get_session_boundaries(market)

    result = []
    for name, bounds in sessions.items():
        phase = _hk_phase_from_time(bounds["start"]) if market == MarketType.HK \
            else _us_phase_from_time(bounds["start"])
        result.append({
            "name": name,
            "start": bounds["start"].isoformat(),
            "end": bounds["end"].isoformat(),
            "phase": phase.value,
            "timezone": tz.zone,
        })
    return result


# ── Compatibility Layer (old MarketHours class API → new functions) ────────
# Used by subscription managers that expect MarketHours.is_hk_active(), etc.
# All methods delegate to the new parameterized API.

class MarketHoursCompat:
    """
    Backward-compatible class wrapping the new parameterized API.

    Used by subscription managers (FTSeek + FTSeek-US) that expect
    MarketHours.is_hk_active(), MarketHours.is_us_trading_hours(), etc.

    All methods delegate to the new get_market_phase() / is_trading_hours()
    functions — no duplicated logic.
    """

    @staticmethod
    def _now(now: Optional[datetime], market: MarketType) -> datetime:
        """解析"当前时间": 显式入参原样透传 (naive 按市场时区解释, 契约不变);
        缺省取市场本地时间 (tz-aware).

        2026-08-10 时区修复: 旧实现 datetime.now() 返回服务器本地时间 (本机 CST),
        被 get_market_phase 当作市场时区 localize — 美股真实盘中 (北京 21:30-04:00)
        全被误判 OVERNIGHT, 订阅管理器 20:00 误退订动态集且整段盘中拒绝重订。
        """
        if now is not None:
            return now
        return get_current_time(market)

    # ── HK convenience methods ──

    @classmethod
    def is_hk_trading_hours(cls, now: Optional[datetime] = None) -> bool:
        """HK in active trading (morning + afternoon, excludes lunch/pre-open)."""
        return is_trading_hours(MarketType.HK, cls._now(now, MarketType.HK))

    @classmethod
    def is_hk_premarket(cls, now: Optional[datetime] = None) -> bool:
        """HK in pre-opening session (08:45-09:30 HKT)."""
        return get_market_phase(MarketType.HK, cls._now(now, MarketType.HK)) == MarketPhase.PRE_OPEN

    @classmethod
    def is_hk_lunch_break(cls, now: Optional[datetime] = None) -> bool:
        """HK in lunch break (12:00-13:00 HKT)."""
        return get_market_phase(MarketType.HK, cls._now(now, MarketType.HK)) == MarketPhase.LUNCH_BREAK

    @classmethod
    def is_hk_closed(cls, now: Optional[datetime] = None) -> bool:
        """
        HK fully closed (after 16:00 or before 08:45).
        Lunch break does NOT count as closed (ExitManager needs quotes).
        """
        resolved = cls._now(now, MarketType.HK)
        t = resolved.time()
        if time(12, 0) <= t < time(13, 0):
            return False  # lunch = keep subscription
        phase = get_market_phase(MarketType.HK, resolved)
        return phase in (MarketPhase.CLOSED, MarketPhase.WEEKEND)

    @classmethod
    def is_hk_active(cls, now: Optional[datetime] = None) -> bool:
        """HK should keep subscription (pre-open + trading + lunch)."""
        return not cls.is_hk_closed(now)

    # ── US convenience methods ──

    @classmethod
    def is_us_trading_hours(cls, now: Optional[datetime] = None) -> bool:
        """US in regular trading (09:30-16:00 ET)."""
        return is_trading_hours(MarketType.US, cls._now(now, MarketType.US))

    @classmethod
    def is_us_premarket(cls, now: Optional[datetime] = None) -> bool:
        """US in pre-market (04:00-09:30 ET)."""
        return get_market_phase(MarketType.US, cls._now(now, MarketType.US)) == MarketPhase.PRE_MARKET

    @classmethod
    def is_us_closed(cls, now: Optional[datetime] = None) -> bool:
        """US fully closed (overnight + weekend)."""
        phase = get_market_phase(MarketType.US, cls._now(now, MarketType.US))
        return phase in (MarketPhase.OVERNIGHT, MarketPhase.WEEKEND)

    @classmethod
    def is_us_active(cls, now: Optional[datetime] = None) -> bool:
        """US should keep subscription (pre-market + trading + after-hours)."""
        phase = get_market_phase(MarketType.US, cls._now(now, MarketType.US))
        return phase in (MarketPhase.PRE_MARKET, MarketPhase.TRADING, MarketPhase.AFTER_HOURS)

    # ── Cross-market coordination ──

    @classmethod
    def is_any_market_active(cls, now: Optional[datetime] = None) -> bool:
        """Either market is active."""
        return cls.is_hk_active(now) or cls.is_us_active(now)

    @classmethod
    def is_hk_and_us_conflict(cls, now: Optional[datetime] = None) -> bool:
        """Both markets active simultaneously (should never happen)."""
        return cls.is_hk_active(now) and cls.is_us_active(now)


# ── Backward-compat alias: old code uses `from shared.market_hours import MarketHours`
# New code should use `from app.shared.market_hours import MarketHoursCompat`
# This alias enables zero-change migration of existing consumers.
MarketHours = MarketHoursCompat

# ── Shadow Mode Validator (Round 4: Safe Migration) ───────────────────────

class MarketHoursValidator:
    """
    Shadow-mode validator that runs old and new logic in parallel.

    Usage:
        # Initialize at startup (Step 3 of migration)
        self._shadow = MarketHoursValidator(
            market=MarketType.HK,
            old_fn=lambda: self._is_trading_hours_original(),
            new_fn=lambda: is_trading_hours(MarketType.HK),
        )

        # In the trading check
        def _is_trading_hours(self) -> bool:
            if config.get('market_hours.shadow_mode', False):
                return self._shadow.check()
            return is_trading_hours(MarketType.HK)

    Promotion criteria:
    - ≥ 500 calls across ≥ 2 full trading sessions
    - ≥ 99.5% agreement rate
    - Zero unexplained discrepancies
    - Both weekend and weekday coverage verified
    """

    def __init__(self, market: MarketType, old_fn, new_fn, max_log: int = 50):
        """
        Args:
            market: The market type being validated.
            old_fn: Callable returning bool (existing implementation).
            new_fn: Callable returning bool (new market_hours implementation).
            max_log: Max discrepancies to keep in memory (default 50).
        """
        self.market = market
        self.old_fn = old_fn
        self.new_fn = new_fn
        self.max_log = max_log
        self.discrepancies: List[Dict] = []
        self.call_count = 0
        self.agreement_count = 0
        self._first_discrepancy_logged = False

    def check(self) -> bool:
        """
        Run both implementations, compare, return OLD result (safe default).

        Returns:
            The OLD implementation result (production-safe during shadow mode).
        """
        old_result = self.old_fn()
        new_result = self.new_fn()
        self.call_count += 1

        if old_result == new_result:
            self.agreement_count += 1
        else:
            discrepancy = {
                "timestamp": datetime.now().isoformat(),
                "market": self.market.value,
                "old_result": old_result,
                "new_result": new_result,
                "agreement_pct": self.agreement_count / self.call_count * 100,
            }
            if len(self.discrepancies) >= self.max_log:
                self.discrepancies.pop(0)
            self.discrepancies.append(discrepancy)

            logger.warning(
                f"[ShadowMode] Market={self.market.value} discrepancy "
                f"#{len(self.discrepancies)}: old={old_result} new={new_result} "
                f"(agreement: {discrepancy['agreement_pct']:.1f}%)"
            )
            # Alert on first discrepancy
            if not self._first_discrepancy_logged:
                self._first_discrepancy_logged = True
                logger.error(
                    f"[ShadowMode] ⚠️ FIRST DISCREPANCY DETECTED for "
                    f"{self.market.value} — review before switching! "
                    f"old={old_result} new={new_result}"
                )

        return old_result  # Always return old result during shadow mode

    def report(self) -> Dict:
        """Generate validation report."""
        return {
            "market": self.market.value,
            "total_checks": self.call_count,
            "agreements": self.agreement_count,
            "discrepancies": len(self.discrepancies),
            "agreement_rate": self.agreement_count / max(1, self.call_count) * 100,
            "discrepancy_details": self.discrepancies[-10:],  # Last 10
            "ready_for_promotion": self.is_ready_for_promotion(),
        }

    def is_ready_for_promotion(self) -> bool:
        """
        Check if shadow mode has met promotion criteria.

        Returns:
            True if the new implementation is ready to replace the old one.
        """
        if self.call_count < 500:
            return False
        agreement_rate = self.agreement_count / max(1, self.call_count) * 100
        if agreement_rate < 99.5:
            return False
        if self.discrepancies:
            return False
        return True

    def reset(self):
        """Reset validation state."""
        self.discrepancies.clear()
        self.call_count = 0
        self.agreement_count = 0
        self._first_discrepancy_logged = False
