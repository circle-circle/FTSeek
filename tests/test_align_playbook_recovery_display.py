"""Playbook 恢复合成 SL/TP 展示值审计化 CAS 对齐契约。"""

import json
import sqlite3

from scripts import align_playbook_recovery_display as align


def _db(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "align.db"))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE open_positions (
            symbol TEXT PRIMARY KEY, owner_strategy TEXT, strategy_id TEXT,
            initial_sl REAL, current_sl REAL, initial_tp REAL,
            take_profit_json TEXT, updated_at TEXT
        );
        CREATE TABLE trade_records (
            id INTEGER PRIMARY KEY, symbol TEXT, source_type TEXT,
            exit_time TEXT, metadata_json TEXT
        );
        """
    )
    return conn


def _insert_position(conn, symbol="US.LLY", owner="playbook_sim",
                     initial_sl=1394.9614, current_sl=1394.9614,
                     initial_tp=1851.99, take_profit_json="[]"):
    conn.execute(
        "INSERT INTO open_positions "
        "(symbol, owner_strategy, strategy_id, initial_sl, current_sl, initial_tp, take_profit_json) "
        "VALUES (?, ?, 'playbook_sim', ?, ?, ?, ?)",
        (symbol, owner, initial_sl, current_sl, initial_tp, take_profit_json),
    )
    conn.execute(
        "INSERT INTO trade_records (id, symbol, source_type, exit_time, metadata_json) "
        "VALUES (1857, ?, 'PLAYBOOK_SIM', NULL, ?)",
        (symbol, json.dumps({"stop_loss": 1352.35, "take_profit": 1530.29})),
    )
    conn.commit()


def test_recovery_pollution_aligned_and_idempotent(tmp_path):
    conn = _db(tmp_path)
    _insert_position(conn)

    rows = align.find_rows(conn)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "US.LLY"
    assert rows[0]["new_initial_sl"] == 1352.35
    assert rows[0]["new_current_sl"] == 1352.35
    assert rows[0]["new_initial_tp"] == 1530.29

    audit = tmp_path / "audit.jsonl"
    assert align.apply_rows(conn, rows, audit) == 1
    row = conn.execute(
        "SELECT initial_sl, current_sl, initial_tp, take_profit_json "
        "FROM open_positions WHERE symbol='US.LLY'"
    ).fetchone()
    assert row["initial_sl"] == 1352.35
    assert row["current_sl"] == 1352.35
    assert row["initial_tp"] == 1530.29
    assert json.loads(row["take_profit_json"]) == [{"ratio": 1.0, "price": 1530.29}]
    assert align.find_rows(conn) == []
    audit_rows = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
    assert audit_rows[0]["applied"] is True and audit_rows[0]["rowcount"] == 1
    conn.close()


def test_trailed_current_sl_not_overwritten(tmp_path):
    conn = _db(tmp_path)
    _insert_position(conn, current_sl=1400.0)
    assert align.find_rows(conn) == []
    conn.close()


def test_existing_tp_json_not_overwritten(tmp_path):
    conn = _db(tmp_path)
    _insert_position(
        conn, take_profit_json=json.dumps([{"ratio": 1.0, "price": 1851.99}])
    )
    assert align.find_rows(conn) == []
    conn.close()


def test_non_playbook_owner_skipped(tmp_path):
    conn = _db(tmp_path)
    _insert_position(conn, owner="manual")
    assert align.find_rows(conn) == []
    conn.close()


def test_missing_truth_skipped(tmp_path):
    conn = _db(tmp_path)
    _insert_position(conn)
    conn.execute(
        "UPDATE trade_records SET metadata_json=? WHERE symbol='US.LLY'",
        (json.dumps({"stop_loss": 1352.35}),),
    )
    conn.commit()
    assert align.find_rows(conn) == []
    conn.close()


def test_cas_rejects_concurrent_value_change(tmp_path):
    conn = _db(tmp_path)
    _insert_position(conn)
    rows = align.find_rows(conn)
    conn.execute(
        "UPDATE open_positions SET current_sl=1400.0 WHERE symbol='US.LLY'"
    )
    conn.commit()
    audit = tmp_path / "audit.jsonl"
    assert align.apply_rows(conn, rows, audit) == 0
    row = conn.execute(
        "SELECT initial_sl, current_sl FROM open_positions WHERE symbol='US.LLY'"
    ).fetchone()
    assert row["initial_sl"] == 1394.9614
    assert row["current_sl"] == 1400.0
    audit_row = json.loads(audit.read_text(encoding="utf-8").strip())
    assert audit_row["applied"] is False and audit_row["rowcount"] == 0
    conn.close()


def test_main_defaults_to_dry_run(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "align.db"
    conn = _db(tmp_path)
    _insert_position(conn)
    monkeypatch.setattr("sys.argv", [
        "align_playbook_recovery_display.py", "--db", str(db_path),
        "--audit-dir", str(tmp_path / "audits"),
    ])
    assert align.main() == 0
    row = conn.execute("SELECT initial_sl, initial_tp FROM open_positions").fetchone()
    assert row["initial_sl"] == 1394.9614 and row["initial_tp"] == 1851.99
    assert not (tmp_path / "audits").exists()
    assert "未写入" in capsys.readouterr().out
    conn.close()
