import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.reconcile_extended_hours_exits import (
    build_reconciliation,
    load_audit_records,
    load_db_state,
)


ORDER_ROWS = [
    {
        "order_id": "8185450", "symbol": "US.NOK", "status": "FILLED_ALL",
        "create_time": "2026-09-17 04:05:12", "qty": 575.0,
        "filled_qty": 575.0, "avg_fill_price": 10.55,
        "price": 10.55, "remark": "ext_exit_lock_in",
    },
    {
        "order_id": "8185451", "symbol": "US.CTNT", "status": "FILLED_ALL",
        "create_time": "2026-09-17 04:10:13",
        "qty": 68493.0, "filled_qty": 68493.0, "avg_fill_price": 0.074,
        "price": 0.073, "remark": "ext_exit_breakeven_lock",
    },
    {
        "order_id": "8185461", "symbol": "US.GIPR", "status": "FILLED_ALL",
        "create_time": "2026-09-17 04:20:13",
        "qty": 11494.0, "filled_qty": 11494.0, "avg_fill_price": 0.424,
        "price": 0.423, "remark": "ext_exit_tighten",
    },
    {
        "order_id": "8185534", "symbol": "US.NBIS", "status": "FILLED_ALL",
        "create_time": "2026-09-17 07:05:23", "qty": 21.0,
        "filled_qty": 21.0, "avg_fill_price": 231.09,
        "price": 230.89, "remark": "ext_exit_breakeven_lock",
    },
]


def _create_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE open_positions (symbol TEXT, direction TEXT, size REAL, "
        "avg_entry_price REAL, entry_time TEXT, owner TEXT, trader_type TEXT, owner_strategy TEXT)"
    )
    conn.execute(
        "CREATE TABLE trade_records (id INTEGER PRIMARY KEY, symbol TEXT, direction TEXT, "
        "entry_price REAL, quantity INTEGER, exit_time TEXT, exit_price REAL, exit_reason TEXT, "
        "close_futu_order_id TEXT, source_type TEXT, owner_strategy TEXT, created_at TEXT, "
        "data_quality TEXT)"
    )
    conn.execute(
        "INSERT INTO trade_records VALUES (1521,'US.CTNT','LONG',0.072,68493,"
        "'2026-09-17T04:15:12-04:00',0.074,'breakeven_lock','8185451','PLAYBOOK_SIM',"
        "'extended_hours_entry','2026-09-17T04:06:14-04:00',NULL)"
    )
    conn.execute(
        "INSERT INTO trade_records VALUES (1515,'US.NOK','LONG',10.335,575,"
        "'2026-09-17T04:05:42-04:00',10.55,'lock_in','8185450','PLAYBOOK_SIM',"
        "'playbook_sim','2026-09-16T21:46:04-04:00',NULL)"
    )
    conn.execute(
        "INSERT INTO trade_records VALUES (1536,'US.NOK','LONG',10.615,555,"
        "'',0,NULL,NULL,'PLAYBOOK_SIM','playbook_sim','2026-09-17T23:04:42+08:00',NULL)"
    )
    conn.execute(
        "INSERT INTO open_positions VALUES ('US.NOK','LONG',555,10.615,"
        "'2026-09-17T23:04:42+08:00','auto','AUTO','playbook_sim')"
    )
    conn.execute(
        "INSERT INTO trade_records VALUES (1522,'US.GIPR','LONG',0.431,11494,"
        "'',0,NULL,NULL,'PLAYBOOK_SIM','extended_hours_entry','2026-09-17T04:15:19-04:00',NULL)"
    )
    conn.execute(
        "INSERT INTO trade_records VALUES (1523,'US.GIPR','LONG',0.431,11494,"
        "'2026-09-17T04:27:01-04:00',0.423,'external_close',NULL,'POSITION_SYNC',NULL,"
        "'2026-09-17T04:27:01-04:00',NULL)"
    )
    conn.execute(
        "INSERT INTO trade_records VALUES (1528,'US.NBIS','LONG',230.05,21,"
        "'2026-09-17T07:08:19-04:00',230.89,'external_close',NULL,'POSITION_SYNC',NULL,"
        "'2026-09-17T07:08:19-04:00','duplicate_external_close')"
    )
    conn.execute(
        "INSERT INTO trade_records VALUES (1524,'US.NBIS','LONG',230.05,21,"
        "'2026-09-17T07:06:15-04:00',231.09,'breakeven_lock','8185534','PLAYBOOK_SIM',"
        "'extended_hours_entry','2026-09-17T06:55:39-04:00',NULL)"
    )
    conn.commit()
    conn.close()


def test_reconciliation_flags_missing_audit_db_close_and_duplicate(tmp_path):
    db_path = tmp_path / "books.db"
    _create_db(db_path)
    audit = {
        "8185450": {"order_id": "8185450", "ok": True, "db_updated": True},
        "8185451": {"order_id": "8185451", "ok": True, "db_updated": True},
        "8185534": {"order_id": "8185534", "ok": True, "db_updated": True},
    }
    report = build_reconciliation(
        ORDER_ROWS, audit,
        load_db_state(db_path, ("US.NOK", "US.CTNT", "US.GIPR", "US.NBIS"))
    )
    by_order = {result["order"]["order_id"]: result for result in report["orders"]}
    assert by_order["8185450"]["issues"] == []
    assert by_order["8185451"]["issues"] == []
    assert by_order["8185534"]["issues"] == []
    assert set(by_order["8185461"]["issues"]) == {
        "audit_missing", "db_close_missing", "duplicate_close_record"
    }
    assert by_order["8185461"]["open_trades"][0]["id"] == 1522
    assert by_order["8185461"]["duplicate_close_candidates"][0]["id"] == 1523
    assert report["summary"]["mismatches"] == 1


def test_audit_loader_keeps_db_updated_record(tmp_path):
    path = tmp_path / "audit.jsonl"
    records = [
        {"order_id": "1", "ok": False, "db_updated": False},
        {"order_id": "1", "ok": True, "db_updated": True},
        {"order_id": "2", "ok": True, "db_updated": True},
    ]
    path.write_text("\n".join(json.dumps(record) for record in records))
    audit = load_audit_records(path)
    assert audit["1"]["db_updated"] is True
    assert set(audit) == {"1", "2"}
