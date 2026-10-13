import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.backfill_fee_net import ensure_columns, repair_duplicate_external_close_rows


def _connection():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE trade_records (
            id INTEGER PRIMARY KEY,
            realized_pnl REAL,
            fees_est REAL,
            pnl_net REAL,
            fees_source TEXT,
            net_pnl_pct REAL,
            data_quality TEXT
        )
        """
    )
    return conn


def test_duplicate_external_close_rows_do_not_receive_net_pnl():
    conn = _connection()
    conn.executemany(
        "INSERT INTO trade_records VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (1, None, 100.82, -100.82, "est", -0.1, "duplicate_external_close"),
            (2, 10.0, 1.0, 9.0, "est", 0.01, None),
        ],
    )

    repaired = repair_duplicate_external_close_rows(conn)

    duplicate = conn.execute("SELECT * FROM trade_records WHERE id=1").fetchone()
    valid = conn.execute("SELECT * FROM trade_records WHERE id=2").fetchone()
    assert repaired == 1
    assert duplicate["fees_est"] is None
    assert duplicate["pnl_net"] is None
    assert duplicate["fees_source"] is None
    assert duplicate["net_pnl_pct"] is None
    assert valid["pnl_net"] == 9.0


def test_ensure_columns_adds_net_pnl_pct_to_legacy_schema():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE trade_records (id INTEGER PRIMARY KEY, realized_pnl REAL)"
    )

    added = ensure_columns(conn)

    columns = {row[1] for row in conn.execute("PRAGMA table_info(trade_records)")}
    assert added == ["fees_est", "pnl_net", "fees_source", "net_pnl_pct"]
    assert {"fees_est", "pnl_net", "fees_source", "net_pnl_pct"} <= columns
