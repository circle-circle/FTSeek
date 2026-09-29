#!/usr/bin/env python3
"""Summarize the RFC-117 UE LONG momentum-gate tri-state audit log."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_LOG_PATH = _ROOT / "data" / "shadow" / "ue_long_momentum_gate.jsonl"
_DECISIONS = ("blocked", "passed", "no_data")


def _read_rows(path: Path) -> tuple[List[Dict[str, Any]], int]:
    rows: List[Dict[str, Any]] = []
    invalid_rows = 0
    if not path.exists():
        return rows, invalid_rows
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            invalid_rows += 1
            continue
        if not isinstance(row, dict):
            invalid_rows += 1
            continue
        rows.append(row)
    return rows, invalid_rows


def _cutoff_date(days: int | None) -> str | None:
    if days is None:
        return None
    now_et = datetime.now(ZoneInfo("America/New_York"))
    return (now_et.date() - timedelta(days=days - 1)).isoformat()


def summarize(path: Path = _DEFAULT_LOG_PATH,
              days: int | None = None,
              since: str | None = None) -> Dict[str, Any]:
    rows, invalid_rows = _read_rows(path)
    cutoff = since or _cutoff_date(days)
    selected = [
        row for row in rows
        if cutoff is None or str(row.get("date", "")) >= cutoff
    ]
    raw_rows = len(selected)
    # 2026-09-29 (复核 P2): inject 的候选循环在 `if dry: return 0` 之前, 且 inject
    #   重跑幂等 — 闸日志是 append, --dry/重跑会对同 (date, symbol) 重复落行,
    #   不去重则 by_date/no_data 占比等分母指标随重跑次数膨胀。
    #   同 (date, symbol) 保留**最后一次**判定 (append 序 = 时间序, 后者最新)。
    dedup: Dict[tuple, Dict[str, Any]] = {}
    for row in selected:
        dedup[(str(row.get("date", "")), str(row.get("symbol", "")))] = row
    selected = list(dedup.values())
    decisions = Counter(str(row.get("decision", "")) for row in selected)
    trends = Counter(str(row.get("trend", "")) for row in selected)
    by_date: Dict[str, Counter] = {}
    for row in selected:
        by_date.setdefault(str(row.get("date", "")), Counter())[str(row.get("decision", ""))] += 1
    return {
        "path": str(path),
        "window_days": days,
        "since": since,
        "valid_rows": len(selected),
        "raw_rows": raw_rows,
        "dup_rows": raw_rows - len(selected),
        "invalid_rows": invalid_rows,
        "trading_dates": sorted(by_date),
        "states_seen": sorted(decisions),
        "by_decision": {decision: decisions[decision] for decision in _DECISIONS},
        "by_trend": dict(sorted(trends.items())),
        "by_date": [
            {
                "date": date,
                "rows": sum(by_date[date].values()),
                "by_decision": {
                    decision: by_date[date][decision] for decision in _DECISIONS
                },
            }
            for date in sorted(by_date)
        ],
    }


def _print_text(report: Dict[str, Any]) -> None:
    print(
        f"UE LONG gate: rows={report['valid_rows']} "
        f"dates={len(report['trading_dates'])} "
        f"blocked={report['by_decision']['blocked']} "
        f"passed={report['by_decision']['passed']} "
        f"no_data={report['by_decision']['no_data']}"
    )
    if report["invalid_rows"]:
        print(f"invalid_rows={report['invalid_rows']}")
    if report["dup_rows"]:
        print(f"dup_rows={report['dup_rows']} (--dry/重跑重复行, 已按 (date,symbol) 去重)")


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", type=Path, default=_DEFAULT_LOG_PATH)
    parser.add_argument("--days", type=int, default=None)
    parser.add_argument("--since", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = summarize(args.path, args.days, args.since)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        _print_text(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
