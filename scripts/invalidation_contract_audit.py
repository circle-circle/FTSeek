#!/usr/bin/env python3
"""Read-only A0 invalidation-contract audit.

Compiles the review-only contract for executed Playbook trades, reproduces the
structured-trigger stop/invalidation buckets, and writes one audit JSONL row
per trade.  No database row is updated and no trading action is emitted.

Usage:
  PYTHONPATH=. ./venv/bin/python scripts/invalidation_contract_audit.py \
      --since 2026-06-26 --until 2026-10-26
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from app.playbook.invalidation_contract import (
    ACTION_REVIEW_ONLY,
    compile_invalidation_contract,
    parse_json_object,
    validate_invalidation_contract,
)
from scripts.stop_invalidation_audit import classify_stop_vs_triggers


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "ftseek_us.db"
DEFAULT_AUDIT_DIR = ROOT / "data" / "audits"


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _instruction_id(metadata: Mapping[str, Any]) -> Optional[int]:
    value = metadata.get("playbook_instruction_id")
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number > 0 else None


def _load_rows(
    conn: sqlite3.Connection,
    since: Optional[str],
    until: Optional[str],
    limit: Optional[int],
) -> tuple[List[Dict[str, Any]], int, int]:
    sql = (
        "SELECT id, symbol, direction, entry_time, realized_pnl, pnl_net, metadata_json "
        "FROM trade_records"
    )
    params: list[Any] = []
    if since:
        sql += " WHERE substr(entry_time, 1, 10) >= ?"
        params.append(since)
    if until:
        sql += (" AND " if since else " WHERE ") + "substr(entry_time, 1, 10) <= ?"
        params.append(until)
    sql += " ORDER BY id"
    trade_rows = [dict(row) for row in conn.execute(sql, params)]
    if limit:
        trade_rows = trade_rows[:limit]

    candidates = 0
    ids = []
    for trade in trade_rows:
        meta = parse_json_object(trade.get("metadata_json"))
        pid = _instruction_id(meta)
        if pid is not None:
            candidates += 1
            ids.append(pid)
    if not ids:
        return [], candidates, 0
    placeholders = ",".join("?" for _ in set(ids))
    instruction_rows = conn.execute(
        "SELECT id, date, symbol, direction, entry_price, stop_loss, invalidation, "
        f"raw_instruction_json, metadata_json FROM playbook_instructions "
        f"WHERE id IN ({placeholders})",
        sorted(set(ids)),
    )
    instructions = {int(row["id"]): dict(row) for row in instruction_rows}
    joined_rows = []
    for trade in trade_rows:
        pid = _instruction_id(parse_json_object(trade.get("metadata_json")))
        if pid in instructions:
            trade["instruction_id"] = pid
            trade["instruction"] = instructions[pid]
            joined_rows.append(trade)
    return joined_rows, candidates, len(joined_rows)


def _audit(
    *,
    db_path: Path,
    since: Optional[str],
    until: Optional[str],
    limit: Optional[int],
    write_audit: bool,
    audit_dir: Path,
) -> Dict[str, Any]:
    conn = _connect(db_path)
    try:
        rows, candidates, joined = _load_rows(conn, since, until, limit)
    finally:
        conn.close()

    source_counts = Counter()
    action_counts = Counter()
    text_status_counts = Counter()
    class_counts = Counter()
    class_pnl = defaultdict(float)
    validation_errors = Counter()
    structured_with_rules = 0
    structured_with_close = 0
    structured_rejected_entries = 0
    contract_rule_count = 0
    legacy_text_parseable_count = 0
    text_contract_usable_count = 0
    out_rows = []

    for trade in rows:
        instruction = trade["instruction"]
        trade_meta = parse_json_object(trade.get("metadata_json"))
        stop_loss = trade_meta.get("stop_loss")
        if stop_loss in (None, ""):
            stop_loss = instruction.get("stop_loss")
        contract = compile_invalidation_contract(
            invalidation=instruction.get("invalidation"),
            raw_instruction_json=instruction.get("raw_instruction_json"),
            direction=trade.get("direction") or instruction.get("direction"),
            entry_price=instruction.get("entry_price"),
        )
        errors = validate_invalidation_contract(contract)
        for error in errors:
            validation_errors[error] += 1
        source_counts[contract["source"]] += 1
        action_counts[contract["action"]] += 1
        text_status_counts[str(contract.get("text_parse_status"))] += 1
        if contract.get("legacy_text_parseable") is True:
            legacy_text_parseable_count += 1
        if contract.get("text_parseable") is True:
            text_contract_usable_count += 1
        contract_rule_count += len(contract.get("rules") or [])

        if contract.get("structured_raw_count", 0) > 0:
            structured_with_rules += 1
            raw = parse_json_object(instruction.get("raw_instruction_json"))
            raw_triggers = raw.get("invalidation_triggers") or []
            if any(
                isinstance(trigger, dict) and trigger.get("metric") == "收盘价"
                for trigger in raw_triggers
            ):
                structured_with_close += 1
            classification = classify_stop_vs_triggers(
                trade.get("direction") or instruction.get("direction") or "",
                stop_loss,
                raw_triggers,
            )
        else:
            classification = {"classification": "no_structured_trigger", "trigger": None,
                              "gap_pct": None, "other_triggers": []}
        structured_rejected_entries += len(contract.get("structured_rejected") or [])
        cls = classification["classification"]
        class_counts[cls] += 1
        pnl = trade.get("pnl_net")
        if pnl is None:
            pnl = trade.get("realized_pnl")
        try:
            pnl_value = float(pnl or 0.0)
        except (TypeError, ValueError):
            pnl_value = 0.0
        class_pnl[cls] += pnl_value
        out_rows.append({
            "trade_id": trade["id"],
            "instruction_id": trade["instruction_id"],
            "symbol": trade["symbol"],
            "direction": trade["direction"],
            "entry_time": trade["entry_time"],
            "pnl_net": round(pnl_value, 2),
            "stop_loss": stop_loss,
            "contract": contract,
            "contract_validation_errors": errors,
            "stop_classification": classification,
        })

    audit_path = None
    if write_audit:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        audit_dir.mkdir(parents=True, exist_ok=True)
        path = audit_dir / f"invalidation_contracts_{timestamp}.jsonl"
        with open(path, "w", encoding="utf-8") as handle:
            for row in out_rows:
                handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        audit_path = str(path)

    structured_classes = {
        key: count for key, count in class_counts.items()
        if key != "no_structured_trigger"
    }
    summary = {
        "window": {"since": since, "until": until},
        "trade_rows_joined": joined,
        "join_candidates": candidates,
        "unjoined": candidates - joined,
        "contract": {
            "schema_version": "invalidation_contract_v1",
            "source_counts": dict(source_counts),
            "action_counts": dict(action_counts),
            "text_status_counts": dict(text_status_counts),
            "legacy_text_parseable_count": legacy_text_parseable_count,
            "text_contract_usable_count": text_contract_usable_count,
            "text_degenerate_count": text_status_counts.get("degenerate", 0),
            "rules_compiled": contract_rule_count,
            "structured_trades": structured_with_rules,
            "structured_with_close_trigger": structured_with_close,
            "structured_entries_rejected": structured_rejected_entries,
            "validation_errors": dict(validation_errors),
        },
        "stop_vs_invalidation": {
            "structured_sample": structured_with_rules,
            "counts": structured_classes,
            "pnl_net_sum": {
                key: round(value, 2) for key, value in sorted(class_pnl.items())
                if key != "no_structured_trigger"
            },
        },
        "a0_action_is_review_only": set(action_counts) <= {ACTION_REVIEW_ONLY},
        "audit_path": audit_path,
    }
    return summary


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="A0 invalidation contract audit (read-only)")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--since", default=None)
    parser.add_argument("--until", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--audit-dir", default=str(DEFAULT_AUDIT_DIR))
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"[invalidation-contract] DB not found: {db_path}")
        return 2
    summary = _audit(
        db_path=db_path,
        since=args.since,
        until=args.until,
        limit=args.limit,
        write_audit=not args.no_write,
        audit_dir=Path(args.audit_dir),
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    if summary["audit_path"]:
        print(f"[invalidation-contract] audit -> {summary['audit_path']}")
    return 0 if (
        not summary["contract"]["validation_errors"] and summary["a0_action_is_review_only"]
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
