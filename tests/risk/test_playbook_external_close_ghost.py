"""Playbook 外部平仓幽灵行契约 (2026-09-17 US.GIPR)."""
import sqlite3


def _create_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE trade_records (
            id INTEGER PRIMARY KEY,
            symbol TEXT,
            direction TEXT,
            trade_date TEXT,
            trade_mode TEXT,
            entry_price REAL,
            quantity INTEGER,
            source_type TEXT,
            decision_id TEXT,
            futu_order_id TEXT,
            exit_reason TEXT,
            exit_price REAL,
            exit_time TEXT
        )
        """
    )
    return conn


def test_external_close_twin_is_not_playbook_open(tmp_path, monkeypatch):
    db_path = tmp_path / "trade_records.db"
    conn = _create_db(db_path)
    conn.executemany(
        """
        INSERT INTO trade_records
        (id, symbol, direction, trade_date, trade_mode, entry_price, quantity,
         source_type, decision_id, futu_order_id, exit_reason, exit_price, exit_time)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        [
            (1, "US.GIPR", "LONG", "2026-09-17", "simulate", 0.431, 11494,
             "PLAYBOOK_SIM", "DA-1", "8185457", None, None, None),
            (2, "US.GIPR", "LONG", "2026-09-17", "simulate", 0.431, 11494,
             "POSITION_SYNC", "", "", "external_close", 0.423, "2026-09-17T04:27:01-04:00"),
        ],
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr("app.core.paths.get_trade_records_db", lambda: db_path)

    from app.playbook.pipeline import is_playbook_open_position, load_playbook_open_symbols

    assert is_playbook_open_position("US.GIPR") is False
    assert load_playbook_open_symbols() == set()


def test_external_close_identity_mismatch_stays_playbook_open(tmp_path, monkeypatch):
    db_path = tmp_path / "trade_records.db"
    conn = _create_db(db_path)
    conn.executemany(
        """
        INSERT INTO trade_records
        (id, symbol, direction, trade_date, trade_mode, entry_price, quantity,
         source_type, decision_id, futu_order_id, exit_reason, exit_price, exit_time)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        [
            (1, "US.GIPR", "LONG", "2026-09-17", "simulate", 0.431, 11494,
             "PLAYBOOK_SIM", "DA-1", "8185457", None, None, None),
            (2, "US.GIPR", "LONG", "2026-09-17", "simulate", 0.431, 1,
             "POSITION_SYNC", "", "", "external_close", 0.423, "2026-09-17T04:27:01-04:00"),
        ],
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr("app.core.paths.get_trade_records_db", lambda: db_path)

    from app.playbook.pipeline import is_playbook_open_position, load_playbook_open_symbols

    assert is_playbook_open_position("US.GIPR") is True
    assert load_playbook_open_symbols() == {"US.GIPR"}
