from datetime import date, timedelta
import sqlite3

import sys

sys.path.insert(0, ".")

from scripts import exit_disable_cf_shadow as sp


def _conn(latest_by_rule):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE trade_records (exit_time TEXT, exit_reason TEXT)")
    for rule, latest in latest_by_rule.items():
        conn.execute(
            "INSERT INTO trade_records(exit_time, exit_reason) VALUES (?, ?)",
            (f"{latest}T10:00:00-04:00", rule))
    return conn


def test_sample_supply_exhaustion_closure(monkeypatch):
    monkeypatch.setattr(
        "app.utils.holiday_calendar.nth_trading_day_after",
        lambda market, start, days: start + timedelta(days=days),
    )
    conn = _conn({"DEAD_SIGNAL_EXIT": "2026-09-01"})
    result = sp.sample_supply_snapshot(
        conn, {"DEAD_SIGNAL_EXIT": 36}, today=date(2026, 9, 22))
    row = result["DEAD_SIGNAL_EXIT"]
    assert row["status"] == "EXHAUSTED_FINAL_READOUT"
    assert row["confidence"] == "limited"
    assert row["exhausted_after"] == "2026-09-21"


def test_sample_supply_active_and_min_n(monkeypatch):
    monkeypatch.setattr(
        "app.utils.holiday_calendar.nth_trading_day_after",
        lambda market, start, days: start + timedelta(days=days),
    )
    conn = _conn({"DEAD_SIGNAL_EXIT": "2026-09-21", "MFE_STALL_EXIT": "2026-09-21"})
    result = sp.sample_supply_snapshot(
        conn, {"DEAD_SIGNAL_EXIT": 36, "MFE_STALL_EXIT": 51},
        today=date(2026, 9, 22))
    assert result["DEAD_SIGNAL_EXIT"]["status"] == "ACTIVE"
    assert result["MFE_STALL_EXIT"]["status"] == "MIN_N_MET"
