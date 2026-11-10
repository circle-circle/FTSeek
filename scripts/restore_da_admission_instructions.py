#!/usr/bin/env python3
"""Re-evaluate untouched DA instructions after an admission policy restore.

Dry-run by default. `--apply` updates only PENDING rows with no trigger, fill,
broker order, execution attempt, or entry_confirm state. No push is emitted.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.playbook.admission import evaluate, get_admission_config
from app.utils.timezone import now_et

DB_PATH = ROOT / "data" / "ftseek_us.db"
AUDIT_DIR = ROOT / "data" / "audits"
ACTIONABLE = {"BUY", "SELL_SHORT"}
POLICY = "2026-11-05-supply-restore"


def _json_object(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _plan(row: sqlite3.Row, stamp: str) -> dict[str, Any] | None:
    if not _eligible(row):
        return None
    raw = _json_object(row["raw_instruction_json"])
    metadata = _json_object(row["metadata_json"])
    previous_admission = raw.get("admission") if isinstance(raw.get("admission"), dict) else {}
    current_direction = str(row["direction"] or "").upper()
    original_direction = str(previous_admission.get("original_direction") or "").upper()
    if current_direction in ACTIONABLE:
        original_direction = current_direction
    if original_direction not in ACTIONABLE:
        return None

    instruction = dict(row)
    instruction["direction"] = original_direction
    instruction["raw_instruction"] = raw
    result = evaluate(instruction)
    previous_skip = str(row["skip_reason"] or "")
    if (result["verdict"] == "PASS" and current_direction in ACTIONABLE
            and previous_skip and not previous_skip.startswith("admission:")):
        return None

    new_direction = original_direction if result["verdict"] == "PASS" else "SKIP"
    new_skip = "" if result["verdict"] == "PASS" else "admission: " + "; ".join(result["reasons"])
    if (new_direction == row["direction"] and new_skip == previous_skip
            and previous_admission.get("reasons") == result["reasons"]):
        return None

    config = get_admission_config(refresh=True)
    restore_record = {
        "at": stamp,
        "policy": POLICY,
        "previous_direction": row["direction"],
        "previous_skip_reason": previous_skip,
        "previous_admission": previous_admission,
        "new_verdict": result["verdict"],
        "new_reasons": result["reasons"],
    }
    raw.setdefault("admission_history", []).append(restore_record)
    raw["admission"] = {
        "verdict": result["verdict"],
        "reasons": result["reasons"],
        "metrics": result["metrics"],
        "enforced": bool(result["enforce"]),
    }
    if result["verdict"] != "PASS":
        raw["admission"]["original_direction"] = original_direction
    metadata["admission_restore"] = {
        "at": stamp,
        "policy": POLICY,
        "min_notional_usd": config.get("min_notional_usd"),
        "max_fee_bps": config.get("max_fee_bps"),
        "previous_direction": row["direction"],
        "previous_skip_reason": previous_skip,
    }
    return {
        "id": row["id"],
        "symbol": row["symbol"],
        "decision_id": row["decision_id"],
        "before": {"direction": row["direction"], "skip_reason": previous_skip},
        "after": {"direction": new_direction, "skip_reason": new_skip},
        "verdict": result["verdict"],
        "reasons": result["reasons"],
        "fee_bps": result["metrics"].get("fee_bps"),
        "raw_instruction_json": json.dumps(raw, ensure_ascii=False),
        "metadata_json": json.dumps(metadata, ensure_ascii=False),
    }


def _eligible(row: sqlite3.Row) -> bool:
    metadata = _json_object(row["metadata_json"])
    direction = str(row["direction"] or "").upper()
    skip_reason = str(row["skip_reason"] or "")
    if direction == "SKIP" and not skip_reason.startswith("admission:"):
        return False
    return (
        row["row_kind"] == "instruction"
        and not int(row["triggered"] or 0)
        and not int(row["executed"] or 0)
        and row["execution_state"] == "PENDING"
        and not int(row["execution_attempt"] or 0)
        and not str(row["broker_order_id"] or "")
        and not metadata.get("entry_confirm")
    )


def restore(date: str, db_path: Path = DB_PATH, *, apply: bool = False,
            audit_dir: Path = AUDIT_DIR) -> dict[str, Any]:
    stamp = now_et().isoformat()
    connection = sqlite3.connect(str(db_path), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    backup_path = None
    try:
        if apply:
            connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            "SELECT * FROM playbook_instructions WHERE date=? AND decision_id LIKE 'DA-%' ORDER BY id",
            (date,),
        ).fetchall()
        plans = [plan for row in rows if (plan := _plan(row, stamp)) is not None]
        result = {
            "date": date,
            "policy": POLICY,
            "applied": apply,
            "candidates": len(rows),
            "changed": len(plans),
            "actionable_after": sum(plan["after"]["direction"] in ACTIONABLE for plan in plans),
            "skip_after": sum(plan["after"]["direction"] == "SKIP" for plan in plans),
            "plans": [{key: value for key, value in plan.items()
                       if key not in {"raw_instruction_json", "metadata_json"}} for plan in plans],
        }
        if not apply:
            return result

        audit_dir.mkdir(parents=True, exist_ok=True)
        operation = stamp.replace(":", "").replace("+", "_")
        backup_path = audit_dir / f"da_admission_restore_{date}_{operation}_before.json"
        backup_path.write_text(json.dumps([dict(row) for row in rows], ensure_ascii=False, indent=2),
                               encoding="utf-8")
        for plan in plans:
            cursor = connection.execute(
                "UPDATE playbook_instructions SET direction=?, skip_reason=?, raw_instruction_json=?, "
                "metadata_json=?, updated_at=? WHERE id=? AND COALESCE(triggered,0)=0 "
                "AND COALESCE(executed,0)=0 AND execution_state='PENDING' "
                "AND COALESCE(execution_attempt,0)=0 AND COALESCE(broker_order_id,'')=''",
                (plan["after"]["direction"], plan["after"]["skip_reason"],
                 plan["raw_instruction_json"], plan["metadata_json"], stamp, plan["id"]),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"instruction {plan['id']} changed concurrently; rolled back")
        connection.commit()
        result["backup"] = str(backup_path)
        audit_path = audit_dir / f"da_admission_restore_{date}_{operation}_result.json"
        audit_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        result["audit"] = str(audit_path)
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True)
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(restore(args.date, args.db, apply=args.apply), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
