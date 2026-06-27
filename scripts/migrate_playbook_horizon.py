#!/usr/bin/env python3
"""
One-shot migration: 为 playbook_instructions 补齐 horizon 列和
playbook_horizon_stats 表，并把已 closed 指令的 horizon 回填为 'short'.

Usage:
    python scripts/migrate_playbook_horizon.py
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.playbook.db import PlaybookDB


def migrate():
    db = PlaybookDB()
    conn = db._conn()
    cursor = conn.cursor()

    # 1. 创建/补齐表和列 (reuse ensure_tables + explicit ALTER for safety)
    db.ensure_tables()

    # 2. 再次显式 ALTER，确保旧 DB 也能加上列
    needed = {
        "horizon": "TEXT DEFAULT 'short'",
        "horizon_confidence": "REAL DEFAULT 0.0",
        "horizon_reasoning": "TEXT",
    }
    existing = {row[1] for row in cursor.execute("PRAGMA table_info(playbook_instructions)").fetchall()}
    for col, col_type in needed.items():
        if col not in existing:
            try:
                cursor.execute(f"ALTER TABLE playbook_instructions ADD COLUMN {col} {col_type}")
                print(f"[migrate] Added column {col}")
            except sqlite3.OperationalError as e:
                print(f"[migrate] Column {col} already exists or error: {e}")

    # 3. 将已有指令（无论是否 closed）回填为 short，避免 NULL 导致退出规则 fallback 歧义
    cursor.execute(
        """
        UPDATE playbook_instructions
        SET horizon = 'short',
            horizon_confidence = COALESCE(horizon_confidence, 0.0),
            horizon_reasoning = COALESCE(horizon_reasoning, '{"source": "migration"}')
        WHERE horizon IS NULL
        """
    )
    print(f"[migrate] Backfilled horizon='short' for {cursor.rowcount} existing instructions (including NULL/holding)")

    conn.commit()
    print("[migrate] Done")


if __name__ == "__main__":
    migrate()
