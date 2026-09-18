import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.backfill_fee_net import repair_duplicate_external_close_rows


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
            data_quality TEXT
        )
        """
    )
    return conn


def test_duplicate_external_close_rows_do_not_receive_net_pnl():
    conn = _connection()
    conn.executemany(
        "INSERT INTO trade_records VALUES (?, ?, ?, ?, ?, ?)",
        [
            (1, None, 100.82, -100.82, "est", "duplicate_external_close"),
            (2, 10.0, 1.0, 9.0, "est", None),
        ],
    )

    repaired = repair_duplicate_external_close_rows(conn)

    duplicate = conn.execute("SELECT * FROM trade_records WHERE id=1").fetchone()
    valid = conn.execute("SELECT * FROM trade_records WHERE id=2").fetchone()
    assert repaired == 1
    assert duplicate["fees_est"] is None
    assert duplicate["pnl_net"] is None
    assert duplicate["fees_source"] is None
    assert valid["pnl_net"] == 9.0
