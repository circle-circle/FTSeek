#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Append an explicit meta_audit finding decision.

This is the human/maintenance gate. It never edits findings.json directly;
meta_audit_loop.py consumes decisions.jsonl on its next run.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(os.environ.get("FTSEEK_REPO", Path(__file__).resolve().parent.parent))
FINDINGS = BASE / "data" / "loops" / "meta_audit" / "findings.json"
DECISIONS = BASE / "data" / "loops" / "meta_audit" / "decisions.jsonl"

DISPOSITIONS = {
    "FIX_NOW", "ACTION_DONE", "VERIFY_PASS", "VERIFY_FAIL",
    "ACCEPT_RISK", "DEFER", "RETIRE", "OUT_OF_SCOPE", "INVALID_SOURCE",
}
PNL_PATHS = {
    "direct_pnl", "capital_preservation", "measurement_validity",
    "cost_reduction", "none",
}
SEVERITIES = {"P0", "P1", "P2", "P3"}


def _validate_date(value: str) -> str:
    datetime.strptime(value, "%Y-%m-%d")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--key", required=True)
    parser.add_argument("--disposition", required=True, choices=sorted(DISPOSITIONS))
    parser.add_argument("--decided-by", required=True)
    parser.add_argument("--rationale", required=True)
    parser.add_argument("--action-owner")
    parser.add_argument("--action-due-by", type=_validate_date)
    parser.add_argument("--review-by", type=_validate_date)
    parser.add_argument("--validation-metric")
    parser.add_argument("--validation-result", choices=("pass", "fail"))
    parser.add_argument("--severity", choices=sorted(SEVERITIES))
    parser.add_argument("--pnl-path", choices=sorted(PNL_PATHS))
    parser.add_argument("--bundle-id")
    parser.add_argument("--closure-mode", choices=("ALL", "PRIMARY_ONLY", "ANY"), default="ALL")
    parser.add_argument("--linked-key", action="append", default=[])
    args = parser.parse_args(argv)

    if not FINDINGS.exists():
        print(f"[meta_audit_decide] findings.json 不存在: {FINDINGS}")
        return 3
    try:
        state = json.loads(FINDINGS.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[meta_audit_decide] findings.json 不可读: {exc}")
        return 2
    if args.key not in state.get("findings", {}):
        print(f"[meta_audit_decide] finding 不存在: {args.key}")
        return 3
    if args.disposition in ("ACCEPT_RISK", "DEFER") and not args.review_by:
        print("[meta_audit_decide] ACCEPT_RISK/DEFER 必须给 --review-by")
        return 2
    if args.disposition == "ACCEPT_RISK" and not args.validation_metric:
        print("[meta_audit_decide] ACCEPT_RISK 必须给 --validation-metric")
        return 2
    if args.disposition == "VERIFY_PASS" and args.validation_result != "pass":
        print("[meta_audit_decide] VERIFY_PASS 必须给 --validation-result pass")
        return 2
    if args.disposition == "VERIFY_FAIL" and args.validation_result != "fail":
        print("[meta_audit_decide] VERIFY_FAIL 必须给 --validation-result fail")
        return 2

    decision = {
        "schema_version": 2,
        "key": args.key,
        "disposition": args.disposition,
        "decided_by": args.decided_by,
        "decided_at": datetime.now().isoformat(timespec="seconds"),
        "rationale": args.rationale,
        "action_owner": args.action_owner,
        "action_due_by": args.action_due_by,
        "review_by": args.review_by,
        "validation_metric": args.validation_metric,
        "validation_result": args.validation_result,
        "severity": args.severity,
        "pnl_path": args.pnl_path,
        "bundle_id": args.bundle_id,
        "closure_mode": args.closure_mode if args.bundle_id else None,
        "linked_keys": args.linked_key or None,
    }
    DECISIONS.parent.mkdir(parents=True, exist_ok=True)
    with DECISIONS.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(decision, ensure_ascii=False) + "\n")
    print(f"[meta_audit_decide] appended {args.key} {args.disposition} → {DECISIONS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
