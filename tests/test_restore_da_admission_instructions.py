import json
import sqlite3

import pytest

from app.playbook import admission
from scripts.restore_da_admission_instructions import restore


SCHEMA = """
CREATE TABLE playbook_instructions (
    id INTEGER PRIMARY KEY, date TEXT, symbol TEXT, direction TEXT, skip_reason TEXT,
    entry_price REAL, stop_loss REAL, take_profit REAL, position_size INTEGER,
    confidence INTEGER, decision_id TEXT, triggered INTEGER DEFAULT 0,
    executed INTEGER DEFAULT 0, execution_state TEXT DEFAULT 'PENDING',
    execution_attempt INTEGER DEFAULT 0, broker_order_id TEXT,
    row_kind TEXT DEFAULT 'instruction', raw_instruction_json TEXT,
    metadata_json TEXT, updated_at TEXT
)
"""


@pytest.fixture(autouse=True)
def _config(monkeypatch):
    monkeypatch.setattr(admission, "get_admission_config", lambda refresh=False: {
        "enabled": True, "enforce": True, "min_notional_usd": 2500.0,
        "max_fee_bps": 11.54, "min_entry_price": 5.0, "max_sl_pct": 8.0,
    })


def _insert(conn, row_id, symbol, direction, skip, price, qty, original,
            *, triggered=0, metadata=None):
    raw = {"claims": [{"claim_type": "earnings_beat"}],
           "supply_selection": {"cohort": "main_pool_base_r1_7"}}
    if original:
        raw["admission"] = {"original_direction": original, "reasons": ["NOTIONAL_TOO_SMALL"]}
    conn.execute(
        "INSERT INTO playbook_instructions (id,date,symbol,direction,skip_reason,entry_price,"
        "stop_loss,take_profit,position_size,confidence,decision_id,triggered,executed,"
        "execution_state,execution_attempt,broker_order_id,row_kind,raw_instruction_json,metadata_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,'PENDING',0,NULL,'instruction',?,?)",
        (row_id, "2026-11-05", symbol, direction, skip, price, price * 0.95, price * 1.08,
         qty, 50, f"DA-test-{symbol}", triggered,
         json.dumps(raw), json.dumps(metadata or {})),
    )


def _db(tmp_path):
    path = tmp_path / "restore.db"
    conn = sqlite3.connect(path)
    conn.execute(SCHEMA)
    _insert(conn, 1, "US.EPAM", "SKIP", "admission: NOTIONAL_TOO_SMALL", 137.36, 36, "BUY")
    _insert(conn, 2, "US.TIC", "BUY", "thesis_drift_invalidate", 10.70, 516, None)
    _insert(conn, 3, "US.RUN", "SKIP", "admission: NOTIONAL_TOO_SMALL", 8.79, 563, "BUY",
            triggered=1)
    _insert(conn, 4, "US.XYZ", "SKIP", "thesis_drift_invalidate", 87.63, 56, None)
    conn.commit()
    conn.close()
    return path


def test_dry_run_then_apply_restores_only_untouched_rows(tmp_path):
    path = _db(tmp_path)
    dry = restore("2026-11-05", path)
    assert dry["applied"] is False and dry["changed"] == 2
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT direction FROM playbook_instructions WHERE id=1").fetchone()[0] == "SKIP"
    conn.close()

    result = restore("2026-11-05", path, apply=True, audit_dir=tmp_path / "audit")
    assert result["changed"] == 2 and result["actionable_after"] == 1
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    epam = conn.execute("SELECT * FROM playbook_instructions WHERE id=1").fetchone()
    tic = conn.execute("SELECT * FROM playbook_instructions WHERE id=2").fetchone()
    assert epam["direction"] == "BUY" and epam["skip_reason"] == ""
    assert json.loads(epam["raw_instruction_json"])["claims"]
    assert tic["direction"] == "SKIP" and "FEE_BPS_TOO_HIGH" in tic["skip_reason"]
    assert conn.execute("SELECT direction FROM playbook_instructions WHERE id=3").fetchone()[0] == "SKIP"
    assert conn.execute("SELECT direction FROM playbook_instructions WHERE id=4").fetchone()[0] == "SKIP"
    conn.close()


def test_second_apply_is_idempotent(tmp_path):
    path = _db(tmp_path)
    restore("2026-11-05", path, apply=True, audit_dir=tmp_path / "audit1")
    second = restore("2026-11-05", path, apply=True, audit_dir=tmp_path / "audit2")
    assert second["changed"] == 0
