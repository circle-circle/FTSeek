"""Offline contracts for the premarket get_rehab read-only tool."""
from datetime import date

import pandas as pd

import scripts.check_corporate_actions as tool


class _QuoteCtx:
    def __init__(self, rows=None, ret=0, error=None):
        self.rows = rows or []
        self.ret = ret
        self.error = error
        self.calls = []

    def get_rehab(self, code):
        self.calls.append(code)
        if self.error:
            raise RuntimeError(self.error)
        return self.ret, pd.DataFrame(self.rows)


def test_event_extraction_covers_dividend_and_split():
    events = tool.events_from_row({
        "per_cash_div": 0.83,
        "split_base": 1.0,
        "split_ert": 4.0,
        "split_ratio": 0.25,
    })
    assert {event["kind"] for event in events} == {"cash_dividend", "split"}


def test_fetch_filters_to_window_and_skips_empty_rows():
    ctx = _QuoteCtx([
        {"ex_div_date": "2026-10-24", "per_cash_div": 0.25},
        {"ex_div_date": "2026-10-20", "split_ratio": 0.5},
        {"ex_div_date": "1998-02-23", "split_base": 1.0, "split_ert": 2.0},
    ])

    result = tool.fetch_corporate_actions(
        ctx, ["us.mu"], start_date=date(2026, 10, 20), end_date=date(2026, 10, 31),
    )

    assert ctx.calls == ["US.MU"]
    assert result["read_only"] is True
    assert [row["ex_date"] for row in result["symbols"]["US.MU"]] == [
        "2026-10-24",
    ]
    assert result["errors"] == {}


def test_fetch_reports_symbol_error_without_raising():
    ctx = _QuoteCtx(error="OpenQuote disconnected")
    result = tool.fetch_corporate_actions(
        ctx, ["US.MU"], start_date=date(2026, 10, 1), end_date=date(2026, 10, 31),
    )
    assert "OpenQuote disconnected" in result["errors"]["US.MU"]
    assert result["symbols"] == {}


def test_get_rehab_failure_is_visible():
    ctx = _QuoteCtx(ret=1)
    result = tool.fetch_corporate_actions(
        ctx, ["US.MU"], start_date=date(2026, 10, 1), end_date=date(2026, 10, 31),
    )
    assert "US.MU" in result["errors"]


def test_main_uses_et_date_for_default_window(monkeypatch):
    captured = {}

    class _Ctx:
        def close(self):
            return None

    def fake_fetch(ctx, symbols, *, start_date, end_date):
        captured.update(symbols=list(symbols), start=start_date, end=end_date)
        return {
            "schema_version": tool.SCHEMA_VERSION,
            "read_only": True,
            "symbols": {},
            "errors": {},
        }

    monkeypatch.setattr(tool, "_today_et", lambda: date(2026, 10, 25))
    monkeypatch.setattr(tool, "fetch_corporate_actions", fake_fetch)
    monkeypatch.setattr("futu.OpenQuoteContext", lambda **kwargs: _Ctx())

    assert tool.main(["us.mu"]) == 0
    assert captured == {
        "symbols": ["US.MU"],
        "start": date(2026, 10, 18),
        "end": date(2026, 11, 8),
    }
