# -*- coding: utf-8 -*-
"""event_reaction_v1 单元测试 (2026-10-21)."""
from datetime import datetime
from zoneinfo import ZoneInfo

from app.event_stream import event_reaction as er

ET = ZoneInfo("America/New_York")


def _bars(*rows):
    return [{"time": day, "open": o, "high": h, "low": l, "close": c}
            for day, o, h, l, c in rows]


def _compute(event_time, symbol, computed_at, bars, quote=None):
    return er.compute_event_reaction(
        event_time=event_time,
        symbols=[symbol],
        computed_at=computed_at,
        bars_by_symbol={symbol: bars},
        quotes_by_symbol={symbol: quote} if quote else {},
    )


def test_hims_old_premarket_news_uses_same_day_close():
    out = _compute(
        "2026-10-20T08:49:00-04:00", "HIMS",
        datetime(2026, 10, 21, 8, 2, tzinfo=ET),
        _bars(("2026-10-19", 28.49, 29.0, 27.53, 28.15),
              ("2026-10-20", 28.0, 30.02, 27.75, 29.21)),
        {"pre_price": 29.36, "price": 29.21},
    )
    row = out["by_symbol"]["HIMS"]
    assert row["event_session"] == "premarket"
    assert row["reaction_basis"] == "same_day_close"
    assert round(row["post_event_move_pct"], 2) == 3.77
    assert 23.0 <= row["age_h"] <= 23.5
    assert "同日收盘" in er.render_event_reaction(out)


def test_nflx_after_close_uses_next_premarket():
    out = _compute(
        "2026-10-20T16:05:00-04:00", "NFLX",
        datetime(2026, 10, 21, 8, 0, tzinfo=ET),
        _bars(("2026-10-19", 69.75, 70.22, 69.02, 69.85),
              ("2026-10-20", 69.84, 70.66, 68.2, 68.69)),
        {"pre_price": 64.13, "price": 68.69},
    )
    row = out["by_symbol"]["NFLX"]
    assert row["event_session"] == "after_hours"
    assert row["reaction_basis"] == "next_premarket"
    assert round(row["post_event_move_pct"], 2) == -6.64
    assert "次日盘前" in er.render_event_reaction(out)


def test_bax_and_cbre_same_day_close_directions():
    bax = _compute(
        "2026-10-20T10:02:00-04:00", "BAX",
        datetime(2026, 10, 21, 8, 2, tzinfo=ET),
        _bars(("2026-10-19", 25.09, 25.45, 24.9, 24.99),
              ("2026-10-20", 25.08, 25.23, 23.62, 24.51)),
    )["by_symbol"]["BAX"]
    assert round(bax["post_event_move_pct"], 2) == -1.92

    cbre = _compute(
        "2026-10-20T13:18:00-04:00", "CBRE",
        datetime(2026, 10, 21, 8, 2, tzinfo=ET),
        _bars(("2026-10-19", 132.73, 133.05, 129.915, 130.77),
              ("2026-10-20", 132.05, 136.68, 130.86, 134.23)),
    )["by_symbol"]["CBRE"]
    assert round(cbre["post_event_move_pct"], 2) == 2.65


def test_after_close_event_scanned_after_open_uses_next_open():
    out = _compute(
        "2026-10-20T16:05:00-04:00", "NFLX",
        datetime(2026, 10, 21, 12, 0, tzinfo=ET),
        _bars(("2026-10-20", 69.84, 70.66, 68.2, 68.69),
              ("2026-10-21", 64.0, 65.0, 63.5, 64.5)),
        {"pre_price": 64.13, "price": 64.8},
    )
    row = out["by_symbol"]["NFLX"]
    assert row["reaction_basis"] == "next_open"
    assert row["reaction_price"] == 64.0


def test_same_day_premarket_event_uses_partial_quote():
    out = _compute(
        "2026-10-21T08:00:00-04:00", "XYZ",
        datetime(2026, 10, 21, 8, 30, tzinfo=ET),
        _bars(("2026-10-20", 10.0, 10.5, 9.8, 10.2)),
        {"pre_price": 10.7, "price": 10.2},
    )
    row = out["by_symbol"]["XYZ"]
    assert row["reaction_basis"] == "same_session_premarket"
    assert round(row["post_event_move_pct"], 2) == 4.9


def test_date_only_event_does_not_guess():
    out = _compute(
        "2026-10-20", "XYZ",
        datetime(2026, 10, 21, 8, 0, tzinfo=ET),
        _bars(("2026-10-20", 10.0, 10.5, 9.8, 10.2)),
    )
    assert out["reason"] == "no_event_time"
    assert out["by_symbol"]["XYZ"]["post_event_move_pct"] is None


def test_missing_bars_is_null_not_zero():
    out = _compute(
        "2026-10-20T10:00:00-04:00", "XYZ",
        datetime(2026, 10, 21, 8, 0, tzinfo=ET),
        [],
    )
    row = out["by_symbol"]["XYZ"]
    assert row["post_event_move_pct"] is None
    assert row["reason"] in ("no_kline", "premarket_not_started")


def test_future_event_is_rejected():
    out = _compute(
        "2026-10-22T10:00:00-04:00", "XYZ",
        datetime(2026, 10, 21, 8, 0, tzinfo=ET),
        _bars(("2026-10-20", 10.0, 10.5, 9.8, 10.2)),
    )
    assert out["reason"] == "future_event"
    assert out["by_symbol"]["XYZ"]["post_event_move_pct"] is None


def test_half_day_close_marks_after_hours(monkeypatch):
    class _Cal:
        def is_trading_day(self, market, day):
            return True

    monkeypatch.setattr(er, "_calendar", lambda: _Cal())
    session = er.classify_event_session(
        datetime(2026, 11, 27, 13, 30, tzinfo=ET))
    assert session == "after_hours"
