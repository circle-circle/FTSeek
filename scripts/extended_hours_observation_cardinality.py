#!/usr/bin/env python3
"""Cardinality and unit audit for extended-hours exit observation fields."""
import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.extended_hours_market_session import FIELD_UNITS
from scripts.shadow_field_cardinality import check_field

SOURCES = {
    "premarket_exit": ROOT / "data" / "premarket_exit_shadow.jsonl",
    "afterhours_exit": ROOT / "data" / "afterhours_exit_shadow.jsonl",
}
FIELDS = (
    "bid_price", "ask_price", "spread_pct", "quote_age_seconds",
    "session_volume_delta", "risk_r_pct", "round_trip_fee_pct",
    "atr_abs", "atr_pct", "atr_source",
    "gross_executable_pnl_pct", "net_executable_pnl_pct",
)


def read_rows(path):
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
        except Exception:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def collect(days=30):
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    results = []
    for source, path in SOURCES.items():
        rows = [
            row for row in read_rows(path)
            if str(row.get("trade_date") or "") >= cutoff
        ]
        for field in FIELDS:
            if field not in FIELD_UNITS:
                raise RuntimeError(f"missing unit declaration: {field}")
            values = [row.get(field) for row in rows if row.get(field) is not None]
            result = check_field(source, field, values)
            result["unit"] = FIELD_UNITS[field]
            results.append(result)
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    results = collect(args.days)
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for result in results:
            print(
                f"{result['source']} {result['field']} n={result['n']} "
                f"unique={result['unique']} unit={result['unit']} "
                f"verdict={result['verdict']}"
            )
    return 1 if any(result["verdict"] == "DEGEN" for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
