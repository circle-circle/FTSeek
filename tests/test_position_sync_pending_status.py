"""EXT pending_sync 状态收口契约（DEB-20261111 Batch 0.2）。"""
from __future__ import annotations

import inspect
import sqlite3
from datetime import datetime

import app.core.position_sync as ps


def _seed(tmp_path, sync_status="pending_sync"):
    db_path = tmp_path / "positions.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE open_positions (symbol TEXT PRIMARY KEY, size REAL, direction TEXT, "
        "sync_status TEXT, last_sync_at TEXT, updated_at TEXT)"
    )
    conn.execute(
        "INSERT INTO open_positions VALUES (?, ?, ?, ?, ?, ?)",
        ("US.AMZN", 18.0, "LONG", sync_status, None, "2026-11-10T16:42:52"),
    )
    conn.commit()
    return conn


def test_pending_sync_flips_when_broker_matches(tmp_path):
    conn = _seed(tmp_path)
    changed = ps._reconcile_pending_sync_status(
        conn.cursor(), "US.AMZN", {"sync_status": "pending_sync"}
    )
    conn.commit()
    row = conn.execute(
        "SELECT sync_status, last_sync_at, updated_at FROM open_positions WHERE symbol='US.AMZN'"
    ).fetchone()
    conn.close()
    assert changed is True
    assert row[0] == "synced"
    assert row[1] is not None
    assert datetime.fromisoformat(row[1]).tzinfo is not None, "last_sync_at 必须使用 aware-ET"
    assert row[2] is not None


def test_synced_row_is_not_rewritten(tmp_path):
    conn = _seed(tmp_path, sync_status="synced")
    cursor = conn.cursor()
    changed = ps._reconcile_pending_sync_status(cursor, "US.AMZN", {"sync_status": "synced"})
    conn.commit()
    row = conn.execute(
        "SELECT sync_status, last_sync_at FROM open_positions WHERE symbol='US.AMZN'"
    ).fetchone()
    conn.close()
    assert changed is False
    assert row == ("synced", None)


def test_cas_does_not_overwrite_concurrent_close(tmp_path):
    conn = _seed(tmp_path)
    conn.execute(
        "UPDATE open_positions SET sync_status='closed_by_sync' WHERE symbol='US.AMZN'"
    )
    conn.commit()
    changed = ps._reconcile_pending_sync_status(
        conn.cursor(), "US.AMZN", {"sync_status": "pending_sync"}
    )
    conn.commit()
    status = conn.execute(
        "SELECT sync_status FROM open_positions WHERE symbol='US.AMZN'"
    ).fetchone()[0]
    conn.close()
    assert changed is False
    assert status == "closed_by_sync"


def test_cas_only_updates_target_symbol(tmp_path):
    conn = _seed(tmp_path)
    conn.execute(
        "INSERT INTO open_positions VALUES ('US.OTHER', 5, 'LONG', 'pending_sync', NULL, NULL)"
    )
    conn.commit()
    assert ps._reconcile_pending_sync_status(
        conn.cursor(), "US.AMZN", {"sync_status": "pending_sync"}) is True
    rows = dict(conn.execute("SELECT symbol, sync_status FROM open_positions").fetchall())
    conn.close()
    assert rows == {"US.AMZN": "synced", "US.OTHER": "pending_sync"}


def test_sync_loop_calls_status_reconcile_before_size_branch():
    source = inspect.getsource(ps.USPositionSync._sync_once)
    reconcile_index = source.index("_reconcile_pending_sync_status(cursor, symbol, db_info)")
    size_index = source.index("size_changed = abs(db_info['size'] - info['qty'])")
    assert reconcile_index < size_index
