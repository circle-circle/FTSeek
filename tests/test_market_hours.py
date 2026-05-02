"""
Unit tests for shared/market_hours.py

Tests cover:
- Timezone correctness (HK=Asia/Hong_Kong, US=America/New_York)
- Phase mapping for all HK and US sessions
- Trading hours logic (incl. lunch break for HK)
- Weekend/holiday detection
- Shadow mode validator
"""

import pytest
from datetime import datetime, date, time
import pytz

# Import the module under test
from app.shared.market_hours import (
    MarketType,
    MarketPhase,
    get_market_phase,
    is_trading_hours,
    is_trading_day,
    get_current_time,
    get_trading_periods,
    MarketHoursValidator,
    _hk_phase_from_time,
    _us_phase_from_time,
)


# ── Timezone Tests ────────────────────────────────────────────────────────

class TestTimezones:
    """Verify timezone resolution is correct for each market."""

    def test_hk_timezone(self):
        """HK should use Asia/Hong_Kong (no DST)."""
        t = get_current_time(MarketType.HK)
        assert t.tzinfo is not None
        assert t.tzinfo.zone == "Asia/Hong_Kong"

    def test_us_timezone(self):
        """US should use America/New_York (with DST)."""
        t = get_current_time(MarketType.US)
        assert t.tzinfo is not None
        assert t.tzinfo.zone == "America/New_York"

    def test_hk_no_dst(self):
        """Hong Kong does not observe DST."""
        # January (winter) and July (summer) should have same UTC offset
        hk = pytz.timezone("Asia/Hong_Kong")
        # Use localize() to avoid pytz LMT (Local Mean Time) issue
        jan = hk.localize(datetime(2026, 1, 15, 12, 0))
        jul = hk.localize(datetime(2026, 7, 15, 12, 0))
        # Both should be UTC+8
        assert jan.utcoffset().total_seconds() == 8 * 3600
        assert jul.utcoffset().total_seconds() == 8 * 3600

    def test_us_has_dst(self):
        """US observes DST — offset differs between winter and summer."""
        et = pytz.timezone("America/New_York")
        # Use localize() to avoid pytz LMT issue
        jan = et.localize(datetime(2026, 1, 15, 12, 0))
        jul = et.localize(datetime(2026, 7, 15, 12, 0))
        # January = EST (UTC-5), July = EDT (UTC-4)
        assert jan.utcoffset().total_seconds() == -5 * 3600
        assert jul.utcoffset().total_seconds() == -4 * 3600


# ── HK Phase Mapping Tests ────────────────────────────────────────────────

class TestHKPhases:
    """Test HK phase mapping against known session times."""

    def test_weekend_is_weekend(self):
        """Saturday/Sunday should return WEEKEND."""
        # 2026-05-02 is Saturday
        sat = datetime(2026, 5, 2, 10, 0, tzinfo=pytz.timezone("Asia/Hong_Kong"))
        sun = datetime(2026, 5, 3, 10, 0, tzinfo=pytz.timezone("Asia/Hong_Kong"))
        assert get_market_phase(MarketType.HK, sat) == MarketPhase.WEEKEND
        assert get_market_phase(MarketType.HK, sun) == MarketPhase.WEEKEND

    def test_pre_open(self):
        """09:00-09:30 should be PRE_OPEN."""
        t = datetime(2026, 5, 4, 9, 15, tzinfo=pytz.timezone("Asia/Hong_Kong"))
        assert get_market_phase(MarketType.HK, t) == MarketPhase.PRE_OPEN

    def test_morning_session(self):
        """09:30-12:00 should be MORNING."""
        t1 = datetime(2026, 5, 4, 9, 30, tzinfo=pytz.timezone("Asia/Hong_Kong"))
        t2 = datetime(2026, 5, 4, 11, 59, tzinfo=pytz.timezone("Asia/Hong_Kong"))
        assert get_market_phase(MarketType.HK, t1) == MarketPhase.MORNING
        assert get_market_phase(MarketType.HK, t2) == MarketPhase.MORNING

    def test_lunch_break(self):
        """12:00-13:00 should be LUNCH_BREAK (HK specific)."""
        t1 = datetime(2026, 5, 4, 12, 0, tzinfo=pytz.timezone("Asia/Hong_Kong"))
        t2 = datetime(2026, 5, 4, 12, 30, tzinfo=pytz.timezone("Asia/Hong_Kong"))
        assert get_market_phase(MarketType.HK, t1) == MarketPhase.LUNCH_BREAK
        assert get_market_phase(MarketType.HK, t2) == MarketPhase.LUNCH_BREAK

    def test_afternoon_session(self):
        """13:00-16:00 should be AFTERNOON."""
        t1 = datetime(2026, 5, 4, 13, 0, tzinfo=pytz.timezone("Asia/Hong_Kong"))
        t2 = datetime(2026, 5, 4, 15, 59, tzinfo=pytz.timezone("Asia/Hong_Kong"))
        assert get_market_phase(MarketType.HK, t1) == MarketPhase.AFTERNOON
        assert get_market_phase(MarketType.HK, t2) == MarketPhase.AFTERNOON

    def test_closed_early_morning(self):
        """Before 09:00 should be CLOSED."""
        t = datetime(2026, 5, 4, 8, 0, tzinfo=pytz.timezone("Asia/Hong_Kong"))
        assert get_market_phase(MarketType.HK, t) == MarketPhase.CLOSED

    def test_closed_after_close(self):
        """After 16:00 should be CLOSED."""
        t = datetime(2026, 5, 4, 17, 0, tzinfo=pytz.timezone("Asia/Hong_Kong"))
        assert get_market_phase(MarketType.HK, t) == MarketPhase.CLOSED

    def test_naive_datetime_handling(self):
        """Naive datetime should be interpreted in market timezone."""
        # Pass naive datetime (no tzinfo)
        naive = datetime(2026, 5, 4, 10, 0)
        phase = get_market_phase(MarketType.HK, naive)
        assert phase == MarketPhase.MORNING


# ── US Phase Mapping Tests ────────────────────────────────────────────────

class TestUSPhases:
    """Test US phase mapping against known session times."""

    def test_weekend_is_weekend(self):
        """Saturday/Sunday should return WEEKEND."""
        sat = datetime(2026, 5, 2, 10, 0, tzinfo=pytz.timezone("America/New_York"))
        assert get_market_phase(MarketType.US, sat) == MarketPhase.WEEKEND

    def test_pre_market(self):
        """04:00-09:30 should be PRE_MARKET."""
        t1 = datetime(2026, 5, 4, 4, 0, tzinfo=pytz.timezone("America/New_York"))
        t2 = datetime(2026, 5, 4, 9, 29, tzinfo=pytz.timezone("America/New_York"))
        assert get_market_phase(MarketType.US, t1) == MarketPhase.PRE_MARKET
        assert get_market_phase(MarketType.US, t2) == MarketPhase.PRE_MARKET

    def test_trading_hours(self):
        """09:30-16:00 should be TRADING."""
        t1 = datetime(2026, 5, 4, 9, 30, tzinfo=pytz.timezone("America/New_York"))
        t2 = datetime(2026, 5, 4, 15, 59, tzinfo=pytz.timezone("America/New_York"))
        assert get_market_phase(MarketType.US, t1) == MarketPhase.TRADING
        assert get_market_phase(MarketType.US, t2) == MarketPhase.TRADING

    def test_after_hours(self):
        """16:00-20:00 should be AFTER_HOURS."""
        t1 = datetime(2026, 5, 4, 16, 0, tzinfo=pytz.timezone("America/New_York"))
        t2 = datetime(2026, 5, 4, 19, 59, tzinfo=pytz.timezone("America/New_York"))
        assert get_market_phase(MarketType.US, t1) == MarketPhase.AFTER_HOURS
        assert get_market_phase(MarketType.US, t2) == MarketPhase.AFTER_HOURS

    def test_overnight(self):
        """20:00-04:00 should be OVERNIGHT."""
        t = datetime(2026, 5, 4, 22, 0, tzinfo=pytz.timezone("America/New_York"))
        assert get_market_phase(MarketType.US, t) == MarketPhase.OVERNIGHT

    def test_early_morning(self):
        """00:00-04:00 should be OVERNIGHT (not PRE_MARKET)."""
        t = datetime(2026, 5, 4, 2, 0, tzinfo=pytz.timezone("America/New_York"))
        assert get_market_phase(MarketType.US, t) == MarketPhase.OVERNIGHT


# ── Trading Hours Tests ───────────────────────────────────────────────────

class TestTradingHours:
    """Test is_trading_hours() for both markets."""

    def test_hk_morning_is_trading(self):
        """HK morning session should be trading."""
        t = datetime(2026, 5, 4, 10, 0, tzinfo=pytz.timezone("Asia/Hong_Kong"))
        assert is_trading_hours(MarketType.HK, t) is True

    def test_hk_afternoon_is_trading(self):
        """HK afternoon session should be trading."""
        t = datetime(2026, 5, 4, 14, 0, tzinfo=pytz.timezone("Asia/Hong_Kong"))
        assert is_trading_hours(MarketType.HK, t) is True

    def test_hk_lunch_not_trading(self):
        """HK lunch break should NOT be trading."""
        t = datetime(2026, 5, 4, 12, 30, tzinfo=pytz.timezone("Asia/Hong_Kong"))
        assert is_trading_hours(MarketType.HK, t) is False

    def test_hk_pre_open_not_trading(self):
        """HK pre-open should NOT be trading."""
        t = datetime(2026, 5, 4, 9, 15, tzinfo=pytz.timezone("Asia/Hong_Kong"))
        assert is_trading_hours(MarketType.HK, t) is False

    def test_hk_weekend_not_trading(self):
        """HK weekend should NOT be trading."""
        t = datetime(2026, 5, 2, 10, 0, tzinfo=pytz.timezone("Asia/Hong_Kong"))
        assert is_trading_hours(MarketType.HK, t) is False

    def test_us_trading_is_trading(self):
        """US regular session should be trading."""
        t = datetime(2026, 5, 4, 10, 0, tzinfo=pytz.timezone("America/New_York"))
        assert is_trading_hours(MarketType.US, t) is True

    def test_us_pre_market_not_trading(self):
        """US pre-market should NOT be trading."""
        t = datetime(2026, 5, 4, 8, 0, tzinfo=pytz.timezone("America/New_York"))
        assert is_trading_hours(MarketType.US, t) is False

    def test_us_after_hours_not_trading(self):
        """US after-hours should NOT be trading."""
        t = datetime(2026, 5, 4, 17, 0, tzinfo=pytz.timezone("America/New_York"))
        assert is_trading_hours(MarketType.US, t) is False


# ── Trading Day Tests ─────────────────────────────────────────────────────

class TestTradingDay:
    """Test is_trading_day() with weekends and holidays."""

    def test_weekday_is_trading_day(self):
        """Monday should be a trading day."""
        monday = date(2026, 5, 4)
        assert is_trading_day(MarketType.HK, monday) is True
        assert is_trading_day(MarketType.US, monday) is True

    def test_saturday_not_trading_day(self):
        """Saturday should not be a trading day."""
        sat = date(2026, 5, 2)
        assert is_trading_day(MarketType.HK, sat) is False
        assert is_trading_day(MarketType.US, sat) is False

    def test_sunday_not_trading_day(self):
        """Sunday should not be a trading day."""
        sun = date(2026, 5, 3)
        assert is_trading_day(MarketType.HK, sun) is False
        assert is_trading_day(MarketType.US, sun) is False

    def test_hk_holiday_not_trading_day(self):
        """HK public holiday should not be a trading day."""
        # 2026-05-01 is Labour Day in HK
        holiday = date(2026, 5, 1)
        assert is_trading_day(MarketType.HK, holiday) is False

    def test_us_holiday_not_trading_day(self):
        """US public holiday should not be a trading day."""
        # 2026-01-19 is MLK Day in US
        holiday = date(2026, 1, 19)
        assert is_trading_day(MarketType.US, holiday) is False

    def test_different_holidays(self):
        """HK and US have different holidays."""
        # 2026-02-17 is HK Lunar New Year, not US holiday
        hk_holiday = date(2026, 2, 17)
        assert is_trading_day(MarketType.HK, hk_holiday) is False
        assert is_trading_day(MarketType.US, hk_holiday) is True


# ── Trading Periods Tests ─────────────────────────────────────────────────

class TestTradingPeriods:
    """Test get_trading_periods() returns correct structure."""

    def test_hk_periods(self):
        """HK should have pre_open, morning, lunch_break, afternoon."""
        periods = get_trading_periods(MarketType.HK)
        names = [p["name"] for p in periods]
        assert "pre_open" in names
        assert "morning" in names
        assert "lunch_break" in names
        assert "afternoon" in names
        # All should have timezone Asia/Hong_Kong
        for p in periods:
            assert p["timezone"] == "Asia/Hong_Kong"

    def test_us_periods(self):
        """US should have pre_market, trading, after_hours."""
        periods = get_trading_periods(MarketType.US)
        names = [p["name"] for p in periods]
        assert "pre_market" in names
        assert "trading" in names
        assert "after_hours" in names
        # All should have timezone America/New_York
        for p in periods:
            assert p["timezone"] == "America/New_York"


# ── Shadow Mode Validator Tests ───────────────────────────────────────────

class TestMarketHoursValidator:
    """Test MarketHoursValidator shadow mode."""

    def test_agreement_count(self):
        """When old and new agree, agreement_count should increase."""
        v = MarketHoursValidator(
            market=MarketType.HK,
            old_fn=lambda: True,
            new_fn=lambda: True,
        )
        v.check()
        v.check()
        assert v.call_count == 2
        assert v.agreement_count == 2
        assert len(v.discrepancies) == 0

    def test_discrepancy_detected(self):
        """When old and new disagree, discrepancy should be logged."""
        v = MarketHoursValidator(
            market=MarketType.US,
            old_fn=lambda: True,
            new_fn=lambda: False,
        )
        result = v.check()
        # Should return OLD result (safe default)
        assert result is True
        assert v.call_count == 1
        assert v.agreement_count == 0
        assert len(v.discrepancies) == 1
        assert v.discrepancies[0]["old_result"] is True
        assert v.discrepancies[0]["new_result"] is False

    def test_report_format(self):
        """Report should contain expected keys."""
        v = MarketHoursValidator(
            market=MarketType.HK,
            old_fn=lambda: True,
            new_fn=lambda: True,
        )
        v.check()
        report = v.report()
        assert "total_checks" in report
        assert "agreements" in report
        assert "discrepancies" in report
        assert "agreement_rate" in report
        assert "ready_for_promotion" in report

    def test_not_ready_under_500_calls(self):
        """Should not be ready for promotion under 500 calls."""
        v = MarketHoursValidator(
            market=MarketType.HK,
            old_fn=lambda: True,
            new_fn=lambda: True,
        )
        for _ in range(499):
            v.check()
        assert v.is_ready_for_promotion() is False

    def test_ready_after_500_agreeing_calls(self):
        """Should be ready for promotion after 500 agreeing calls."""
        v = MarketHoursValidator(
            market=MarketType.HK,
            old_fn=lambda: True,
            new_fn=lambda: True,
        )
        for _ in range(500):
            v.check()
        assert v.is_ready_for_promotion() is True

    def test_not_ready_with_discrepancy(self):
        """Should not be ready if any discrepancies exist."""
        v = MarketHoursValidator(
            market=MarketType.HK,
            old_fn=lambda: True,
            new_fn=lambda: True,
        )
        for _ in range(499):
            v.check()
        # Introduce one discrepancy
        v.old_fn = lambda: True
        v.new_fn = lambda: False
        v.check()
        assert v.is_ready_for_promotion() is False

    def test_reset(self):
        """Reset should clear all state."""
        v = MarketHoursValidator(
            market=MarketType.HK,
            old_fn=lambda: True,
            new_fn=lambda: True,
        )
        v.check()
        v.check()
        v.reset()
        assert v.call_count == 0
        assert v.agreement_count == 0
        assert len(v.discrepancies) == 0

    def test_max_log_limit(self):
        """Should not store more than max_log discrepancies."""
        v = MarketHoursValidator(
            market=MarketType.HK,
            old_fn=lambda: True,
            new_fn=lambda: False,
            max_log=5,
        )
        for _ in range(10):
            v.check()
        assert len(v.discrepancies) == 5  # Only last 5 kept


# ── Edge Case Tests ───────────────────────────────────────────────────────

class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_exact_boundary_0930_hk(self):
        """09:30:00 should be MORNING (not PRE_OPEN)."""
        t = datetime(2026, 5, 4, 9, 30, 0, tzinfo=pytz.timezone("Asia/Hong_Kong"))
        assert get_market_phase(MarketType.HK, t) == MarketPhase.MORNING

    def test_exact_boundary_1200_hk(self):
        """12:00:00 should be LUNCH_BREAK (not MORNING)."""
        t = datetime(2026, 5, 4, 12, 0, 0, tzinfo=pytz.timezone("Asia/Hong_Kong"))
        assert get_market_phase(MarketType.HK, t) == MarketPhase.LUNCH_BREAK

    def test_exact_boundary_1600_us(self):
        """16:00:00 should be AFTER_HOURS (not TRADING)."""
        t = datetime(2026, 5, 4, 16, 0, 0, tzinfo=pytz.timezone("America/New_York"))
        assert get_market_phase(MarketType.US, t) == MarketPhase.AFTER_HOURS

    def test_midnight_hk(self):
        """00:00 HKT should be CLOSED."""
        t = datetime(2026, 5, 4, 0, 0, tzinfo=pytz.timezone("Asia/Hong_Kong"))
        assert get_market_phase(MarketType.HK, t) == MarketPhase.CLOSED

    def test_midnight_us(self):
        """00:00 ET should be OVERNIGHT."""
        t = datetime(2026, 5, 4, 0, 0, tzinfo=pytz.timezone("America/New_York"))
        assert get_market_phase(MarketType.US, t) == MarketPhase.OVERNIGHT
