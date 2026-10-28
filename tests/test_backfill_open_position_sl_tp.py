"""open_positions 一次性 TP/SL 回填脚本契约 (DEB-20261028)."""

import json
import sqlite3

from scripts import backfill_open_position_sl_tp as bf


def _db(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "backfill.db"))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE open_positions (
            symbol TEXT PRIMARY KEY, owner_strategy TEXT, initial_sl REAL,
            current_sl REAL, initial_tp REAL, take_profit_json TEXT,
            entry_time TEXT, updated_at TEXT
        );
        CREATE TABLE trade_records (
            id INTEGER PRIMARY KEY, symbol TEXT, source_type TEXT,
            exit_time TEXT, entry_time TEXT, metadata_json TEXT
        );
        """
    )
    return conn


def test_backfill_fills_missing_tp_and_is_idempotent(tmp_path):
    conn = _db(tmp_path)
    conn.execute(
        "INSERT INTO open_positions "
        "(symbol, owner_strategy, initial_sl, current_sl, initial_tp, take_profit_json, entry_time) "
        "VALUES ('US.CRM', 'playbook_sim', 232.32, 232.32, NULL, NULL, "
        "'2026-10-27T09:51:03.458732-04:00')"
    )
    conn.execute(
        "INSERT INTO trade_records "
        "(id, symbol, source_type, exit_time, entry_time, metadata_json) "
        "VALUES (1729, 'US.CRM', 'PLAYBOOK_SIM', NULL, "
        "'2026-10-27T09:51:03.458732-04:00', ?)",
        (json.dumps({"stop_loss": 232.32, "take_profit": 262.89}),),
    )
    conn.commit()

    rows = bf.find_rows(conn)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "US.CRM"
    assert rows[0]["new_initial_tp"] == 262.89
    assert json.loads(rows[0]["new_take_profit_json"]) == [
        {"ratio": 1.0, "price": 262.89}
    ]
    assert "missing_tp" in rows[0]["reason"]

    audit = tmp_path / "audit.jsonl"
    assert bf.apply_rows(conn, rows, audit) == 1
    row = conn.execute(
        "SELECT initial_tp, take_profit_json FROM open_positions WHERE symbol='US.CRM'"
    ).fetchone()
    assert row["initial_tp"] == 262.89
    assert json.loads(row["take_profit_json"])[0]["price"] == 262.89
    assert bf.find_rows(conn) == []
    assert audit.read_text(encoding="utf-8").strip()
    conn.close()


def test_backfill_does_not_overwrite_existing_tp(tmp_path):
    conn = _db(tmp_path)
    conn.execute(
        "INSERT INTO open_positions "
        "(symbol, owner_strategy, initial_sl, current_sl, initial_tp, take_profit_json, entry_time) "
        "VALUES ('US.KEEP', 'playbook_sim', 0, 0, 300.0, "
        "'[{\"ratio\": 1.0, \"price\": 300.0}]', "
        "'2026-10-27T09:51:03.458732-04:00')"
    )
    conn.execute(
        "INSERT INTO trade_records "
        "(id, symbol, source_type, exit_time, entry_time, metadata_json) "
        "VALUES (1, 'US.KEEP', 'PLAYBOOK_SIM', NULL, "
        "'2026-10-27T09:51:03.458732-04:00', ?)",
        (json.dumps({"stop_loss": 232.32, "take_profit": 262.89}),),
    )
    conn.commit()

    rows = bf.find_rows(conn)
    assert len(rows) == 1
    assert rows[0]["new_initial_tp"] == 300.0
    assert json.loads(rows[0]["new_take_profit_json"])[0]["price"] == 300.0
    assert "missing_tp" not in rows[0]["reason"]
    assert "missing_sl" in rows[0]["reason"]
    conn.close()
