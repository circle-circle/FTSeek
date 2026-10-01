#!/usr/bin/env python3
"""Archive and remove orphan playbook_user_feedback rows.

The production DB contains historical feedback rows whose instruction_id no
longer exists. They fail PRAGMA foreign_key_check and can make referential
integrity reports noisy. This one-off tool reports them by default; --apply
writes an append-only JSONL audit snapshot before deleting only those rows.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "ftseek_us.db"
DEFAULT_AUDIT_DIR = ROOT / "data" / "audits"


def find_orphans(db_path: Path) -> List[Dict[str, object]]:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT f.id, f.instruction_id, f.action, f.skip_reason, "
            "f.modified_field, f.original_value, f.modified_value, f.created_at "
            "FROM playbook_user_feedback f "
            "LEFT JOIN playbook_instructions i ON i.id = f.instruction_id "
            "WHERE i.id IS NULL ORDER BY f.id"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def apply_repair(
    db_path: Path,
    orphan_rows: List[Dict[str, object]],
    audit_path: Path,
) -> None:
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_path.open("w", encoding="utf-8") as audit_file:
        for row in orphan_rows:
            audit_file.write(json.dumps(row, ensure_ascii=False) + "\n")

    if not orphan_rows:
        return

    row_ids = [row["id"] for row in orphan_rows]
    placeholders = ",".join("?" for _ in row_ids)
    connection = sqlite3.connect(db_path)
    try:
        with connection:
            connection.execute(
                "DELETE FROM playbook_user_feedback "
                f"WHERE id IN ({placeholders})",
                row_ids,
            )
    finally:
        connection.close()


def foreign_key_violations(db_path: Path) -> List[tuple]:
    connection = sqlite3.connect(db_path)
    try:
        return connection.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="delete orphan rows; without this flag the tool is read-only",
    )
    parser.add_argument(
        "--audit-file",
        type=Path,
        default=DEFAULT_AUDIT_DIR
        / (
            "playbook_user_feedback_orphans_"
            + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            + ".jsonl"
        ),
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    orphan_rows = find_orphans(args.db)
    result = {
        "db": str(args.db),
        "mode": "apply" if args.apply else "dry-run",
        "orphan_count": len(orphan_rows),
        "audit_file": str(args.audit_file),
        "rows": orphan_rows,
    }
    if args.apply:
        apply_repair(args.db, orphan_rows, args.audit_file)
        result["remaining_orphan_count"] = len(find_orphans(args.db))

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(
            f"mode={result['mode']} orphan_count={result['orphan_count']} "
            f"audit_file={result['audit_file']}"
        )
        for row in orphan_rows:
            print(
                f"id={row['id']} instruction_id={row['instruction_id']} "
                f"action={row['action']} created_at={row['created_at']}"
            )
        if args.apply:
            print(f"remaining_orphan_count={result['remaining_orphan_count']}")
        else:
            print("dry-run: no DB changes; pass --apply to repair")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
