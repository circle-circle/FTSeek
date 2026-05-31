#!/usr/bin/env python3
"""
P0-2: Migrate all database timestamps from CST (UTC+8) to ET/EDT (UTC-4).
Offset: -12 hours (EDT period, all data from April 24+ is after spring forward).

Usage:
    python scripts/migrate_cst_to_et.py --dry-run   # Preview only
    python scripts/migrate_cst_to_et.py              # Execute migration
"""
import sqlite3
import sys
from datetime import datetime, timedelta

OFFSET = timedelta(hours=12)  # CST(UTC+8) → EDT(UTC-4) = -12h
DRY_RUN = '--dry-run' in sys.argv


def migrate_timestamp(ts_str):
    """Convert CST naive ISO timestamp to ET naive ISO timestamp."""
    if not ts_str or not isinstance(ts_str, str):
        return ts_str
    try:
        dt = datetime.fromisoformat(ts_str)
        return (dt - OFFSET).isoformat()
    except (ValueError, TypeError):
        return ts_str


def migrate_trade_records():
    """Migrate ftseek_us.db trade_records: entry_time, exit_time."""
    db_path = 'data/ftseek_us.db'
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.execute("SELECT count(*) FROM trade_records WHERE entry_time IS NOT NULL")
    total = cur.fetchone()[0]

    rows = cur.execute("SELECT rowid, entry_time, exit_time FROM trade_records").fetchall()
    migrated = 0
    for rowid, entry_time, exit_time in rows:
        new_entry = migrate_timestamp(entry_time)
        new_exit = migrate_timestamp(exit_time)
        if new_entry != entry_time or new_exit != exit_time:
            if not DRY_RUN:
                cur.execute(
                    "UPDATE trade_records SET entry_time=?, exit_time=? WHERE rowid=?",
                    (new_entry, new_exit, rowid)
                )
            migrated += 1

    if not DRY_RUN:
        conn.commit()

    # Verify
    cur.execute("SELECT symbol, entry_time, exit_time FROM trade_records ORDER BY rowid DESC LIMIT 3")
    samples = cur.fetchall()
    conn.close()

    print(f"trade_records: {migrated}/{total} rows {'would be ' if DRY_RUN else ''}migrated")
    for row in samples:
        print(f"  {row[0]} | entry={row[1]} | exit={row[2]}")
    return migrated


def migrate_decisions():
    """Migrate decisions.db: decisions.created_at."""
    db_path = 'data/decisions.db'
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.execute("SELECT count(*) FROM decisions WHERE created_at IS NOT NULL")
    total = cur.fetchone()[0]

    rows = cur.execute("SELECT rowid, created_at FROM decisions").fetchall()
    migrated = 0
    for rowid, created_at in rows:
        new_ca = migrate_timestamp(created_at)
        if new_ca != created_at:
            if not DRY_RUN:
                cur.execute("UPDATE decisions SET created_at=? WHERE rowid=?", (new_ca, rowid))
            migrated += 1

    if not DRY_RUN:
        conn.commit()

    cur.execute("SELECT decision_id, symbol, created_at FROM decisions ORDER BY created_at DESC LIMIT 3")
    samples = cur.fetchall()
    conn.close()

    print(f"\ndecisions: {migrated}/{total} rows {'would be ' if DRY_RUN else ''}migrated")
    for row in samples:
        print(f"  {row[0]} | {row[1]} | {row[2]}")
    return migrated


def migrate_signal_lifecycle():
    """Migrate ftseek_us.db signal_lifecycle timestamps if any."""
    db_path = 'data/ftseek_us.db'
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # Check if table has timestamp columns
    try:
        cur.execute("PRAGMA table_info(signal_lifecycle)")
        cols = [c[1] for c in cur.fetchall()]
    except Exception:
        print("\nsignal_lifecycle: table not found, skipping")
        conn.close()
        return 0

    migrated = 0
    for ts_col in ['entry_time', 'exit_time', 'created_at', 'trade_date']:
        if ts_col in cols:
            rows = cur.execute(
                f"SELECT rowid, {ts_col} FROM signal_lifecycle WHERE {ts_col} IS NOT NULL"
            ).fetchall()
            for rowid, val in rows:
                new_val = migrate_timestamp(val)
                if new_val != val:
                    if not DRY_RUN:
                        cur.execute(
                            f"UPDATE signal_lifecycle SET {ts_col}=? WHERE rowid=?",
                            (new_val, rowid)
                        )
                    migrated += 1

    if not DRY_RUN:
        conn.commit()
    conn.close()

    if migrated:
        print(f"\nsignal_lifecycle: {migrated} timestamp fields {'would be ' if DRY_RUN else ''}migrated")
    return migrated


if __name__ == '__main__':
    print(f"{'[DRY RUN] ' if DRY_RUN else ''}CST → ET Migration (offset: -12h)\n")

    m1 = migrate_trade_records()
    m2 = migrate_decisions()
    m3 = migrate_signal_lifecycle()

    total = m1 + m2 + m3
    print(f"\n{'=' * 40}")
    print(f"Total: {total} {'rows would be ' if DRY_RUN else ''}migrated")
    if DRY_RUN:
        print("Run without --dry-run to execute migration.")
