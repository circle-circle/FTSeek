import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.repair_extended_hours_exit_books import apply_repair_plan, build_repair_plan


def _create_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE trade_records (id INTEGER PRIMARY KEY, trade_date TEXT, symbol TEXT, "
        "direction TEXT, entry_time TEXT, exit_time TEXT, entry_price REAL, exit_price REAL, "
        "quantity INTEGER, realized_pnl REAL, pnl_pct REAL, exit_reason TEXT, "
        "close_futu_order_id TEXT, source_type TEXT, owner_strategy TEXT, created_at TEXT, "
        "data_quality TEXT, metadata_json TEXT)"
    )
    conn.execute(
        "INSERT INTO trade_records VALUES (1522,'2026-09-17','US.GIPR','LONG',"
        "'2026-09-17T04:15:19-04:00','',0.431,0,11494,NULL,NULL,NULL,NULL,'PLAYBOOK_SIM',"
        "'extended_hours_entry','2026-09-17T04:15:19-04:00',NULL,NULL)"
    )
    conn.execute(
        "INSERT INTO trade_records VALUES (1523,'2026-09-17','US.GIPR','LONG',"
        "'2026-09-17T04:21:59-04:00','2026-09-17T04:27:01-04:00',0.431,0.423,11494,"
        "-91.95,-0.0186,'external_close',NULL,'POSITION_SYNC',NULL,"
        "'2026-09-17T04:27:01-04:00',NULL,NULL)"
    )
    conn.commit()
    conn.close()


def _report():
    return {
        "generated_at": "2026-09-17T23:23:17Z",
        "orders": [{
            "order": {
                "order_id": "8185461", "symbol": "US.GIPR", "status": "FILLED_ALL",
                "create_time": "2026-09-17 04:20:13", "qty": 11494.0,
                "filled_qty": 11494.0, "avg_fill_price": 0.424,
                "price": 0.423, "remark": "ext_exit_tighten", "session": "ETH",
            },
            "issues": ["audit_missing", "db_close_missing", "duplicate_close_record"],
            "open_trades": [{
                "id": 1522, "symbol": "US.GIPR", "direction": "LONG",
                "entry_price": 0.431, "quantity": 11494,
            }],
            "duplicate_close_candidates": [{"id": 1523}],
        }],
    }


def test_repair_plan_closes_original_and_marks_duplicate(tmp_path):
    actions = build_repair_plan(_report())
    assert [action["action"] for action in actions] == [
        "append_reconciliation_audit", "repair_open_trade",
        "mark_duplicate_external_close",
    ]
    repair = next(action for action in actions if action["action"] == "repair_open_trade")
    assert repair["exit_price"] == 0.424
    assert repair["realized_pnl"] == -80.46


def test_apply_repair_is_idempotent_and_writes_audit(tmp_path):
    db_path = tmp_path / "books.db"
    audit_path = tmp_path / "audit.jsonl"
    _create_db(db_path)
    actions = build_repair_plan(_report())
    first = apply_repair_plan(db_path, audit_path, actions, "2026-09-17T23:23:17Z")
    second = apply_repair_plan(db_path, audit_path, actions, "2026-09-17T23:23:17Z")
    conn = sqlite3.connect(db_path)
    original = conn.execute(
        "SELECT exit_price, exit_reason, close_futu_order_id, realized_pnl, "
        "json_extract(metadata_json, '$.extended_hours_reconciliation.order_id') "
        "FROM trade_records WHERE id=1522"
    ).fetchone()
    duplicate = conn.execute(
        "SELECT data_quality FROM trade_records WHERE id=1523"
    ).fetchone()[0]
    conn.close()
    assert original == (0.424, "tighten", "8185461", -80.46, "8185461")
    assert duplicate == "duplicate_external_close"
    assert len(first["audit_appended"]) == 1
    assert second["audit_appended"] == []
