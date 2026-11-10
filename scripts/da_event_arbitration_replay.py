#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.event_stream.instructions import arbitrate_intraday_card


def _write_json_new(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def _supply(rows: List[Dict[str, Any]], direction_key: str) -> int:
    return sum(row[direction_key] in {"BUY", "SELL_SHORT"} for row in rows)


def _summary(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    baseline = _supply(rows, "baseline_direction")
    structured = _supply(rows, "structured_direction")
    arms = {}
    for arm in sorted({str(row.get("arm") or "UNKNOWN") for row in rows}):
        arm_rows = [row for row in rows if str(row.get("arm") or "UNKNOWN") == arm]
        arm_baseline = _supply(arm_rows, "baseline_direction")
        arm_structured = _supply(arm_rows, "structured_direction")
        arms[arm] = {
            "valid_cards": len(arm_rows),
            "baseline_actionable": arm_baseline,
            "structured_actionable": arm_structured,
            "actionable_supply_delta": arm_structured - arm_baseline,
            "changed_cards": sum(row["changed"] for row in arm_rows),
        }
    return {
        "valid_cards": len(rows),
        "baseline_actionable": baseline,
        "structured_actionable": structured,
        "actionable_supply_delta": structured - baseline,
        "changed_cards": sum(row["changed"] for row in rows),
        "reason_counts": dict(sorted(Counter(row["reason"] for row in rows).items())),
        "by_arm": arms,
        "economic_value_claimed": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline R3 arbitration for VALID R2 cards")
    parser.add_argument("--replay-dir", type=Path, action="append", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    batches = []
    all_rows = []
    for replay_dir in args.replay_dir:
        rows = []
        degraded = 0
        for path in sorted(replay_dir.glob("US.*.*.json")):
            if path.name.endswith(".comparison.json"):
                continue
            card = json.loads(path.read_text(encoding="utf-8"))["card"]
            if card["status"] != "VALID":
                degraded += 1
                continue
            row = arbitrate_intraday_card(card) | {
                "arm": card["arm"],
                "source_replay": str(replay_dir),
                "source_file": path.name,
            }
            rows.append(row)
            all_rows.append(row)
            _write_json_new(args.output_dir / f"{replay_dir.name}.{path.stem}.json", row)
        batches.append({
            "replay_dir": str(replay_dir),
            "degraded_cards_excluded": degraded,
            **_summary(rows),
        })
    summary = {
        "schema": "da_event_arbitration_replay_v1",
        "batches": batches,
        "combined": _summary(all_rows),
        "production_write": False,
        "notifier_calls": 0,
    }
    _write_json_new(args.output_dir / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
