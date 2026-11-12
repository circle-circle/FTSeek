#!/usr/bin/env python3
"""US overnight record-only 影子只读汇总；不取行情、不写文件、不计算交易收益。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIR = ROOT / "data" / "shadow" / "us_overnight"


def evaluate_file(path: Path) -> dict:
    if not path.exists():
        return {"schema": "us_overnight_shadow_eval_v1", "status": "NO_DATA",
                "exit_code": 1, "path": str(path), "n_rows": 0}
    rows = []
    bad_rows = 0
    for line in path.read_text(encoding="utf-8", errors="strict").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("row is not an object")
            rows.append(row)
        except (ValueError, TypeError):
            bad_rows += 1
    if not rows:
        return {"schema": "us_overnight_shadow_eval_v1", "status": "NO_DATA",
                "exit_code": 1, "path": str(path), "n_rows": 0,
                "bad_rows": bad_rows}
    rows.sort(key=lambda row: str(row.get("ts") or ""))
    latest = {}
    for row in rows:
        symbol = str(row.get("symbol") or "")
        if symbol:
            latest[symbol] = row
    return {
        "schema": "us_overnight_shadow_eval_v1",
        "status": "OK",
        "exit_code": 0,
        "path": str(path),
        "session_date": path.stem,
        "n_rows": len(rows),
        "n_symbols": len(latest),
        "first_ts": rows[0].get("ts"),
        "last_ts": rows[-1].get("ts"),
        "bad_rows": bad_rows,
        "latest_by_symbol": [latest[key] for key in sorted(latest)],
        "caveat": "record-only quote snapshots; no orders, no fills, no PnL or graduation verdict",
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    parser.add_argument("--date", help="session date YYYY-MM-DD；缺省取最新文件")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    files = sorted(args.dir.glob("*.jsonl")) if args.dir.exists() else []
    path = (args.dir / f"{args.date}.jsonl") if args.date else (files[-1] if files else args.dir / "NO_DATA.jsonl")
    try:
        result = evaluate_file(path)
    except (OSError, UnicodeError) as exc:
        result = {"schema": "us_overnight_shadow_eval_v1", "status": "READ_ERROR",
                  "exit_code": 1, "path": str(path), "error": str(exc)}
    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"status={result['status']} path={result['path']} "
              f"rows={result.get('n_rows', 0)} symbols={result.get('n_symbols', 0)} "
              f"bad_rows={result.get('bad_rows', 0)}")
    return int(result["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
