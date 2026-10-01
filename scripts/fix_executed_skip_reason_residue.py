#!/usr/bin/env python3
"""Repair historical executed rows with residual skip_reason state.

The write-path invariant added on 2026-09-09 clears skip_reason whenever
executed becomes 1. Existing rows written before that fix can still contain
both values, which makes consumers count booked trades as skipped.
This one-off tool reports those rows by default and only mutates the DB with
--apply. It writes an audit JSONL file containing the pre-repair state.
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


def find_residue(db_path: Path) -> List[Dict[str, object]]:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT id, date, symbol, direction, executed, skip_reason, "
            "execution_price, outcome, updated_at "
            "FROM playbook_instructions "
            "WHERE COALESCE(executed, 0) = 1 "
            "AND COALESCE(skip_reason, '') <> '' "
            "ORDER BY date, id"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def apply_repair(
    db_path: Path,
    rows: List[Dict[str, object]],
    audit_path: Path,
) -> None:
    if not rows:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text("", encoding="utf-8")
        return

    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_path.open("w", encoding="utf-8") as audit_file:
        for row in rows:
            audit_file.write(json.dumps(row, ensure_ascii=False) + "\n")

    row_ids = [row["id"] for row in rows]
    now = datetime.now(timezone.utc).isoformat()
    connection = sqlite3.connect(db_path)
    try:
        with connection:
            placeholders = ",".join("?" for _ in row_ids)
            connection.execute(
                "UPDATE playbook_instructions "
                f"SET skip_reason = '', updated_at = ? WHERE id IN ({placeholders})",
                (now, *row_ids),
            )
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="mutate rows; without this flag the tool is read-only",
    )
    parser.add_argument(
        "--audit-file",
        type=Path,
        default=DEFAULT_AUDIT_DIR
        / f"executed_skip_reason_residue_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.jsonl",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args()

    rows = find_residue(args.db)
    result = {
        "db": str(args.db),
        "mode": "apply" if args.apply else "dry-run",
        "residue_count": len(rows),
        "audit_file": str(args.audit_file),
        "rows": rows,
    }

    if args.apply:
        apply_repair(args.db, rows, args.audit_file)
        result["remaining_count"] = len(find_residue(args.db))

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"mode={result['mode']} residue_count={result['residue_count']}")
        for row in rows:
            print(
                f"{row['date']} {row['symbol']} id={row['id']} "
                f"skip_reason={row['skip_reason']}"
            )
        if args.apply:
            print(f"remaining_count={result['remaining_count']}")
            print(f"audit_file={result['audit_file']}")
        else:
            print("dry-run: no DB changes; pass --apply to repair")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
