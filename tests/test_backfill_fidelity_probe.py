import importlib.util
import json
import sqlite3
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backfill_fidelity_probe.py"
SPEC = importlib.util.spec_from_file_location("backfill_fidelity_probe", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE trade_records (id INTEGER, symbol TEXT, trade_date TEXT,
        entry_price REAL, market_regime TEXT, metadata_json TEXT, source_type TEXT,
        owner_strategy TEXT, entry_reason TEXT, exit_price REAL)""")
    conn.execute("CREATE TABLE playbook_instructions (id INTEGER, market_regime TEXT, atr_pct REAL)")
    return conn


def _backfill(conn, rid, iid, regime="RANGE", proxy_atr=10.0, price=100.0):
    conn.execute("INSERT INTO trade_records VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (rid, "US.X", "2026-11-10", price, regime,
                  json.dumps({"playbook_instruction_id": iid, "atr": proxy_atr}),
                  "PLAYBOOK_SIM", "playbook_sim", "sync_recovery_backfill", 101.0))


def test_probe_quantifies_regime_gap_and_instruction_truth():
    conn = _conn()
    _backfill(conn, 1, iid=10, proxy_atr=10.0, price=100.0)
    conn.execute("INSERT INTO playbook_instructions VALUES (?,?,?)", (10, "cautious", 0.04))
    conn.execute("INSERT INTO trade_records VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (2, "US.Y", "2026-11-10", 100.0, "cautious", "{}",
                  "PLAYBOOK_SIM", "playbook_sim", "playbook_futu_simulate", 101.0))
    report = MODULE.probe(conn, "2026-11-01")
    assert report["n_backfill"] == 1
    assert report["n_joined_instruction"] == 1
    assert report["regime_fidelity_current_pct"] == 0.0
    assert report["instruction_regime_available_pct"] == 100.0
    assert report["regime_would_fix_n"] == 1
    assert report["instruction_atr_available_pct"] == 100.0
    assert report["detail"][0]["instruction_real_atr"] == 4.0
    assert report["atr_median_abs_diff"] == 6.0


def test_probe_handles_missing_instruction():
    conn = _conn()
    _backfill(conn, 1, iid=999)
    report = MODULE.probe(conn, "2026-11-01")
    assert report["n_backfill"] == 1
    assert report["n_joined_instruction"] == 0
    assert report["regime_fidelity_current_pct"] is None


def test_probe_regime_match_when_already_correct():
    conn = _conn()
    _backfill(conn, 1, iid=10, regime="cautious")
    conn.execute("INSERT INTO playbook_instructions VALUES (?,?,?)", (10, "cautious", 0.04))
    report = MODULE.probe(conn, "2026-11-01")
    assert report["regime_fidelity_current_pct"] == 100.0
    assert report["regime_would_fix_n"] == 0
