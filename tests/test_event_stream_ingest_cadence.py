"""EventStream 分时采频 (2026-10-18): 关键窗 5m / 盘中 15m / 其余 20m."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from scripts.event_stream_ingest import (
    _is_us_trading_day,
    _read_last_success,
    _write_last_success,
    cadence_minutes,
    should_run,
)

ET = ZoneInfo("America/New_York")


def _dt(hour, minute, day=16):
    return datetime(2026, 10, day, hour, minute, tzinfo=ET)


def test_key_window_keeps_five_minutes():
    assert cadence_minutes(_dt(7, 30), True) == 5
    assert cadence_minutes(_dt(9, 34), True) == 5


def test_trading_day_offpeak_is_fifteen_minutes():
    assert cadence_minutes(_dt(6, 0), True) == 15
    assert cadence_minutes(_dt(10, 0), True) == 15
    assert cadence_minutes(_dt(19, 59), True) == 15


def test_overnight_and_weekend_are_twenty_minutes():
    assert cadence_minutes(_dt(2, 0), True) == 20
    assert cadence_minutes(_dt(8, 0, day=17), False) == 20


def test_should_run_uses_last_success_age():
    now = _dt(10, 0)
    assert should_run(now, None, 15) is True
    assert should_run(now, now - timedelta(minutes=14), 15) is False
    assert should_run(now, now - timedelta(minutes=15), 15) is True
    assert should_run(now, now - timedelta(minutes=1), 15, force=True) is True


def test_state_roundtrip_and_corrupt_fails_open(tmp_path):
    path = tmp_path / "state.json"
    now = _dt(10, 0)
    _write_last_success(now, path)
    assert _read_last_success(path) == now
    path.write_text("{not-json", encoding="utf-8")
    assert _read_last_success(path) is None


def test_is_us_trading_day_uses_no_network_holiday_check(monkeypatch):
    class Calendar:
        def __init__(self, holiday):
            self._holiday = holiday

        def is_us_holiday(self, day):
            return self._holiday

        def is_trading_day(self, *args, **kwargs):
            raise AssertionError("cadence must not call network trading-day API")

    monkeypatch.setattr(
        "app.utils.holiday_calendar.get_holiday_calendar",
        lambda: Calendar(False),
    )
    assert _is_us_trading_day(date(2026, 11, 11)) is True

    monkeypatch.setattr(
        "app.utils.holiday_calendar.get_holiday_calendar",
        lambda: Calendar(True),
    )
    assert _is_us_trading_day(date(2026, 11, 26)) is False
