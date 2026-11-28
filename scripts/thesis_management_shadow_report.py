#!/usr/bin/env python3
"""PULLBACK_LIMIT 承接失败影子读数（只读，绝不改交易）。

默认只打印 JSON；--out 显式写汇总且拒绝覆盖既有文件。
本脚本只报覆盖/触发，不捏造提前量、误杀或 paired PnL；这些需后续前向结算。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "data" / "shadow" / "thesis_management_shadow.jsonl"
DEFAULT_PATH_INPUT = ROOT / "data" / "shadow" / "thesis_management_path.jsonl"


def summarize(path: Path, forward_path: Path | None = None) -> dict:
    rows = []
    bad_lines = 0
    if path.exists():
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except (TypeError, ValueError, json.JSONDecodeError):
                    bad_lines += 1
                    continue
                if row.get("schema_version") == "thesis_management_shadow_v1":
                    rows.append(row)
    decisions = Counter(str(row.get("decision") or "MISSING") for row in rows)
    reasons = Counter(str(row.get("reason") or "MISSING") for row in rows)
    failed_ids = {row.get("trade_id") for row in rows
                  if row.get("decision") == "FAILED_RECLAIM"}
    trade_ids = {row.get("trade_id") for row in rows}
    et_days = {str(row.get("known_at") or "")[:10] for row in rows
               if row.get("decision") == "FAILED_RECLAIM"}
    et_days.discard("")
    forward_rows = []
    if forward_path is not None and forward_path.exists():
        with forward_path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except (TypeError, ValueError, json.JSONDecodeError) as _bad_row_err:
                    print(f"[thesis_mgmt_shadow] forward 坏行跳过: {_bad_row_err}", file=sys.stderr)
                    continue
                if row.get("schema_version") == "thesis_management_path_v1":
                    forward_rows.append(row)
    return {
        "schema": "thesis_management_shadow_report_v1",
        "input": str(path),
        "forward_path": {
            "input": str(forward_path) if forward_path is not None else None,
            "bars": len(forward_rows),
            "unique_trades": len({row.get("trade_id") for row in forward_rows}),
            "last_known_at": max((str(row.get("known_at") or "") for row in forward_rows), default=None),
        },
        "rows": len(rows),
        "bad_lines": bad_lines,
        "unique_trades": len(trade_ids),
        "decisions": dict(sorted(decisions.items())),
        "reasons": dict(sorted(reasons.items())),
        "failed_reclaim": {
            "unique_trades": len(failed_ids),
            "et_days": len(et_days),
            "min_required_trades": 30,
            "min_required_et_days": 10,
            "mechanism_sample_met": len(failed_ids) >= 30 and len(et_days) >= 10,
        },
        "scope": "record-only；只报覆盖/触发，未结算 paired Δ 时不得作盈利宣称；blocked/缺报价窗按 CENSORED/缺口处理",
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--path-input", type=Path, default=DEFAULT_PATH_INPUT)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    result = summarize(args.input, args.path_input)
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        if args.out.exists():
            print(f"拒绝覆盖既有文件: {args.out}", file=sys.stderr)
            return 2
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
