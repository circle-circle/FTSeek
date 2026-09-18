import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.extended_hours_profit_exit_rules import (
    decide_original_profit_exit,
    prioritize_profit_exit,
    persist_profit_exit_state,
)


META = {
    "stop_loss": 95.0,
    "take_profit": 110.0,
    "conditions": [],
    "peak_price": 100.0,
}


def test_profit_exit_signal_has_priority_over_fallback():
    assert prioritize_profit_exit("TAKE_PROFIT", "pre_close_flatten") == "TAKE_PROFIT"
    assert prioritize_profit_exit("SL_BREAKEVEN", "hold_to_next_open") == "SL_BREAKEVEN"
    assert prioritize_profit_exit("SL_TRAIL_LOCK", "hold_to_next_open") == "SL_TRAIL_LOCK"
    assert prioritize_profit_exit("hold", "pre_close_flatten") == "pre_close_flatten"
    assert prioritize_profit_exit("hold") == "hold"


def test_breakeven_exits_only_after_arming_and_net_floor_breach():
    unearned = decide_original_profit_exit(
        "LONG", 100.0, 99.0, 95.0, 100.0, {**META, "peak_price": 100.0}, 0.15
    )
    assert unearned["action"] == "hold_no_stop"

    armed = decide_original_profit_exit(
        "LONG", 100.0, 100.8, 95.0, 101.0, {**META, "peak_price": 101.0}, 0.15
    )
    assert armed["action"] == "hold"
    assert armed["breakeven_triggered"] is True

    breached = decide_original_profit_exit(
        "LONG", 100.0, 100.3, 95.0, 101.0,
        {**armed["metadata"], "peak_price": 101.0}, 0.15,
    )
    assert breached["action"] == "SL_BREAKEVEN"
    assert breached["metadata"]["_be_net_line_price"] == pytest.approx(100.15)


def test_trail_lock_ratchets_from_favorable_peak():
    decision = decide_original_profit_exit(
        "LONG", 100.0, 102.0, 95.0, 103.0,
        {**META, "peak_price": 103.0, "breakeven_triggered": True}, 0.15,
    )
    assert decision["action"] == "SL_TRAIL_LOCK"
    assert decision["effective_sl"] == pytest.approx(102.176, abs=0.001)
    assert decision["peak_profit_pct"] == pytest.approx(3.0)


def test_take_profit_uses_original_rr_gate_and_executable_price():
    decision = decide_original_profit_exit(
        "LONG", 100.0, 110.2, 95.0, 110.0, META, 0.15
    )
    assert decision["action"] == "TAKE_PROFIT"
    assert decision["take_profit_rr"] == pytest.approx(2.0)

    low_rr = decide_original_profit_exit(
        "LONG", 100.0, 110.2, 109.0, 110.0,
        {**META, "stop_loss": 109.0}, 0.15,
    )
    assert low_rr["action"] == "hold"
    assert low_rr["detail"] == "take_profit_rr_gate"


def test_take_profit_rejects_gross_profit_below_round_trip_fee():
    decision = decide_original_profit_exit(
        "LONG", 100.0, 100.2, 95.0, 100.0,
        {**META, "stop_loss": 99.9, "take_profit": 100.2}, 0.25,
    )
    assert decision["action"] == "hold"
    assert decision["detail"] == "take_profit_below_net_fee"
    assert decision["net_profit_pct"] == pytest.approx(-0.05)


def test_short_uses_ask_and_favorable_low_peak():
    decision = decide_original_profit_exit(
        "SHORT", 100.0, 97.7, 105.0, 97.0,
        {**META, "stop_loss": 105.0, "take_profit": 90.0, "peak_price": 97.0},
        0.15,
    )
    assert decision["action"] == "hold"
    assert decision["peak_price"] == pytest.approx(97.0)
    assert decision["peak_profit_pct"] == pytest.approx(3.0)


def test_peak_state_persists_without_overwriting_unrelated_metadata(tmp_path):
    db_path = tmp_path / "trades.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE trade_records (id INTEGER PRIMARY KEY, metadata_json TEXT)"
    )
    conn.execute(
        "INSERT INTO trade_records VALUES (1, ?)",
        (json.dumps({"unrelated": "keep", "peak_price": 100.0}),),
    )
    conn.commit()
    conn.close()

    decision = decide_original_profit_exit(
        "LONG", 100.0, 103.0, 95.0, 100.0,
        {"stop_loss": 95.0, "peak_price": 100.0}, 0.15,
    )
    assert persist_profit_exit_state(db_path, 1, decision, "LONG")

    conn = sqlite3.connect(db_path)
    metadata = json.loads(conn.execute(
        "SELECT metadata_json FROM trade_records WHERE id=1"
    ).fetchone()[0])
    conn.close()
    assert metadata["unrelated"] == "keep"
    assert metadata["peak_price"] == pytest.approx(103.0)
    assert metadata["breakeven_triggered"] is True
