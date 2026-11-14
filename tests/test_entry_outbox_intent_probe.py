import importlib.util
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "entry_outbox_intent_probe.py"
SPEC = importlib.util.spec_from_file_location("entry_outbox_intent_probe", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def _conn(with_voting=True):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE trade_records (id INTEGER, symbol TEXT, trade_date TEXT,
        entry_time TEXT, created_at TEXT, entry_price REAL, quantity INTEGER, market_regime TEXT,
        metadata_json TEXT, source_type TEXT, owner_strategy TEXT, entry_reason TEXT, exit_price REAL)""")
    conn.execute("""CREATE TABLE playbook_instructions (id INTEGER, market_regime TEXT, atr_pct REAL,
        execution_price REAL, stop_loss REAL, take_profit REAL, conditions_json TEXT, thesis TEXT,
        horizon TEXT, decision_id TEXT)""")
    if with_voting:
        conn.execute("CREATE TABLE voting_predictions (decision_id TEXT)")
    return conn


def _window_ts(delta_sec):
    entry_et = "2026-10-26T09:41:55-04:00"
    entry_utc = datetime.fromisoformat(entry_et).astimezone(timezone.utc)
    created_naive = (entry_utc + timedelta(seconds=delta_sec)).astimezone().replace(tzinfo=None)
    return entry_et, created_naive.isoformat()


def _backfill(conn, rid, iid, regime="RANGE", proxy_atr=10.0, price=100.0, delta_sec=30):
    entry_time, created_at = _window_ts(delta_sec)
    conn.execute("INSERT INTO trade_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (rid, "US.X", "2026-11-10", entry_time, created_at, price, 10, regime,
                  json.dumps({"playbook_instruction_id": iid, "atr": proxy_atr}),
                  "PLAYBOOK_SIM", "playbook_sim", "sync_recovery_backfill", 101.0))


def _instruction(conn, iid, regime="cautious", atr_pct=0.04, execution_price=100.0,
                 stop_loss=95.0, take_profit=110.0, conditions='["earnings_beat"]',
                 thesis="catalyst", horizon="short", decision_id="DA-20261026-US.X-aaaa"):
    conn.execute("INSERT INTO playbook_instructions VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (iid, regime, atr_pct, execution_price, stop_loss, take_profit,
                  conditions, thesis, horizon, decision_id))


def test_core_intent_complete_and_clean_replayable():
    conn = _conn()
    _backfill(conn, 1, iid=10, proxy_atr=10.0, price=100.0)
    _instruction(conn, 10)
    report = MODULE.probe(conn, "2026-11-01")
    assert report["n_backfill"] == 1
    assert report["n_joined_instruction"] == 1
    assert report["core_intent_complete_n"] == 1
    assert report["core_intent_complete_pct"] == 100.0
    assert report["clean_replayable_pct"] == 100.0
    assert report["full_intent_complete_pct"] == 100.0
    assert report["detail"][0]["instruction_real_atr"] == 4.0
    assert report["intent_availability_pct"]["regime"] == 100.0


def test_window_gap_timezone_correct():
    conn = _conn()
    _backfill(conn, 1, iid=10, delta_sec=30)
    _instruction(conn, 10)
    report = MODULE.probe(conn, "2026-11-01")
    window = report["orphan_exposure_window_sec"]
    assert window["n"] == 1
    assert window["p50"] == 30.0
    assert window["max"] == 30.0
    assert window["5_60s"] == 1
    assert report["detail"][0]["orphan_exposure_sec"] == 30.0


def test_atr_would_fix_proxy_vs_real():
    conn = _conn()
    _backfill(conn, 1, iid=10, proxy_atr=10.0, price=100.0)
    _instruction(conn, 10, atr_pct=0.04)
    report = MODULE.probe(conn, "2026-11-01")
    assert report["real_atr_would_fix_n"] == 1
    assert report["atr_median_abs_diff"] == 6.0


def test_voting_na_for_da_not_in_core():
    conn = _conn()
    _backfill(conn, 1, iid=10)
    _instruction(conn, 10, decision_id="DA-20261026-US.X-aaaa")
    report = MODULE.probe(conn, "2026-11-01")
    assert report["intent_availability_pct"]["voting"] == 0.0
    assert report["core_intent_complete_pct"] == 100.0
    assert report["full_intent_complete_pct"] == 100.0


def test_full_intent_requires_extras():
    conn = _conn()
    _backfill(conn, 1, iid=10)
    _instruction(conn, 10, thesis="", conditions="[]")
    report = MODULE.probe(conn, "2026-11-01")
    assert report["core_intent_complete_pct"] == 100.0
    assert report["full_intent_complete_pct"] == 0.0


def test_missing_instruction_skipped_but_window_kept():
    conn = _conn()
    _backfill(conn, 1, iid=999, delta_sec=45)
    report = MODULE.probe(conn, "2026-11-01")
    assert report["n_backfill"] == 1
    assert report["n_joined_instruction"] == 0
    assert report["core_intent_complete_pct"] is None
    assert report["orphan_exposure_window_sec"]["n"] == 1
    assert report["orphan_exposure_window_sec"]["p50"] == 45.0


def test_missing_voting_table_no_crash():
    conn = _conn(with_voting=False)
    _backfill(conn, 1, iid=10)
    _instruction(conn, 10)
    report = MODULE.probe(conn, "2026-11-01")
    assert report["intent_availability_pct"]["voting"] == 0.0
    assert report["core_intent_complete_pct"] == 100.0


def test_signal_rows_excluded():
    conn = _conn()
    _backfill(conn, 1, iid=10)
    _instruction(conn, 10)
    entry_time, created_at = _window_ts(30)
    conn.execute("INSERT INTO trade_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (2, "US.Y", "2026-11-10", entry_time, created_at, 100.0, 10, "cautious", "{}",
                  "PLAYBOOK_SIM", "playbook_sim", "playbook_futu_simulate", 101.0))
    report = MODULE.probe(conn, "2026-11-01")
    assert report["n_backfill"] == 1
