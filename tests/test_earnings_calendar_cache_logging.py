"""财报日历缓存/日志优化测试 (2026-10-19)."""
import logging
from datetime import datetime, timedelta

from app.data.earnings_calendar import EarningsCalendar


def _rows(label="AAA", date="2026-10-20"):
    return [{
        "symbol": label, "reportDate": date,
        "timeOfTheDay": "post-market", "estimate": "1.0", "source": "futu",
    }]


def test_calendar_fingerprint_stable_and_changes():
    a = _rows("AAA", "2026-10-20")
    a_reordered = list(reversed(a))
    b = _rows("BBB", "2026-10-20")
    assert EarningsCalendar._calendar_fingerprint(a) == \
        EarningsCalendar._calendar_fingerprint(a_reordered)
    assert EarningsCalendar._calendar_fingerprint(a) != \
        EarningsCalendar._calendar_fingerprint(b)


def test_cache_hit_does_not_refetch(monkeypatch):
    ec = EarningsCalendar(api_key="test")
    calls = []
    monkeypatch.setattr(
        ec, "_fetch_futu_calendar_primary",
        lambda **kw: (calls.append("futu"), _rows())[1])
    monkeypatch.setattr(ec, "_fetch_raw_calendar", lambda horizon="3month": "")
    rows1, _ = ec.get_earnings_calendar_with_status()
    rows2, _ = ec.get_earnings_calendar_with_status()
    assert rows1 == rows2
    assert calls == ["futu"]


def test_force_refresh_refetches(monkeypatch):
    ec = EarningsCalendar(api_key="test")
    calls = []
    monkeypatch.setattr(
        ec, "_fetch_futu_calendar_primary",
        lambda **kw: (calls.append("futu"), _rows())[1])
    monkeypatch.setattr(ec, "_fetch_raw_calendar", lambda horizon="3month": "")
    ec.get_earnings_calendar_with_status(force_refresh=True)
    ec.get_earnings_calendar_with_status(force_refresh=True)
    assert calls == ["futu", "futu"]


def test_cache_expiry_refetches_and_unchanged_is_debug(monkeypatch, caplog):
    ec = EarningsCalendar(api_key="test")
    calls = []
    monkeypatch.setattr(
        ec, "_fetch_futu_calendar_primary",
        lambda **kw: (calls.append("futu"), _rows())[1])
    monkeypatch.setattr(ec, "_fetch_raw_calendar", lambda horizon="3month": "")

    with caplog.at_level(logging.DEBUG, logger="ftseek.us_pipeline.earnings_calendar"):
        ec.get_earnings_calendar_with_status()
        first_info = sum(1 for r in caplog.records if "获取到" in r.message)
        ec._cache_time = datetime.now() - timedelta(seconds=3601)
        caplog.clear()
        ec.get_earnings_calendar_with_status()
        second_info = sum(1 for r in caplog.records if "获取到" in r.message)
        second_debug = sum(1 for r in caplog.records if "无变化" in r.message)

    assert calls == ["futu", "futu"]
    assert first_info == 1
    assert second_info == 0
    assert second_debug == 1
