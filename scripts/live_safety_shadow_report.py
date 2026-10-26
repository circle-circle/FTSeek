#!/usr/bin/env python3
"""On-demand evaluator for broker capacity and broker STOP drift shadows."""
from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_jsonl(path: Path) -> tuple[list[dict], int]:
    rows = []
    malformed = 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return rows, 0
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except (TypeError, ValueError):
            malformed += 1
            continue
        if isinstance(row, dict):
            rows.append(row)
        else:
            malformed += 1
    return rows, malformed


def summarize_capacity(rows: list[dict]) -> dict:
    status_counts = Counter(str(row.get("capacity_status") or "unknown") for row in rows)
    intent_counts = Counter(str(row.get("intent") or "") for row in rows)
    error_counts = Counter(str(row.get("capacity_error") or "") for row in rows if row.get("capacity_error"))
    external = [row for row in rows if int(row.get("external_active_order_count") or 0) > 0]
    insufficient = [row for row in rows if row.get("capacity_status") == "insufficient"]
    return {
        "n": len(rows),
        "status_counts": dict(status_counts),
        "intent_counts": dict(intent_counts),
        "unique_symbols": len({row.get("symbol") for row in rows if row.get("symbol")}),
        "insufficient_rate_pct": round(len(insufficient) / len(rows) * 100.0, 2) if rows else None,
        "insufficient_sample": insufficient[:10],
        "rows_with_external_active_orders": len(external),
        "capacity_error_counts": dict(error_counts.most_common(10)),
    }


def summarize_drift(rows: list[dict]) -> dict:
    observed = [
        row for row in rows
        if row.get("observation_status") == "observed" and row.get("abs_drift_bps") is not None
    ]
    values = sorted(float(row["abs_drift_bps"]) for row in observed)
    unavailable = sum(1 for row in rows if row.get("observation_status") != "observed")
    return {
        "n": len(rows),
        "observed": len(observed),
        "unavailable": unavailable,
        "unique_stop_ids": len({row.get("stop_order_id") for row in rows if row.get("stop_order_id")}),
        "abs_drift_bps_median": round(statistics.median(values), 2) if values else None,
        "abs_drift_bps_p90": round(values[min(len(values) - 1, int(len(values) * 0.9))], 2) if values else None,
        "abs_drift_bps_max": round(values[-1], 2) if values else None,
        "drift_gt_1bp": sum(1 for value in values if value > 1.0),
        "drift_gt_10bp": sum(1 for value in values if value > 10.0),
        "status_counts": dict(Counter(str(row.get("observation_status") or "unknown") for row in rows)),
    }


def build_report(capacity_path: Path, drift_path: Path) -> dict:
    capacity_rows, capacity_malformed = _load_jsonl(capacity_path)
    drift_rows, drift_malformed = _load_jsonl(drift_path)
    return {
        "schema_version": "live_safety_shadow_report.v1",
        "read_only": True,
        "capacity": {
            **summarize_capacity(capacity_rows),
            "path": str(capacity_path),
            "malformed": capacity_malformed,
        },
        "stop_drift": {
            **summarize_drift(drift_rows),
            "path": str(drift_path),
            "malformed": drift_malformed,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--capacity-path", type=Path,
                        default=ROOT / "data" / "broker_capacity_shadow.jsonl")
    parser.add_argument("--drift-path", type=Path,
                        default=ROOT / "data" / "shadow" / "broker_stop_drift.jsonl")
    args = parser.parse_args(argv)
    print(json.dumps(build_report(args.capacity_path, args.drift_path),
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
