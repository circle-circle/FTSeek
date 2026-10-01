#!/usr/bin/env python3
"""Replay the 2026-09 direction-skew guardrail day-vision claim.

The historical DB is post-hoc state, not an event log. This script therefore
reports two explicit arms:
  * doc_unfiltered: counts every BUY/SELL_SHORT row, including terminal rows.
    This reproduces the historical 45 -> 19 / rescue 26 count, but violates the
    DEB's own INVALIDATED_SKIP_REASONS filter.
  * filter_terminal: applies the DEB's stated prior rule.

All rescued-PnL figures are gross entry-to-close and depend on the tie-break
order because every historical skew-cut row has confidence=70.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Dict, Iterable, List, Tuple


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "ftseek_us.db"
DEFAULT_BARS = ROOT / "data" / "replay_bars_daily"
TERMINAL_SKIP_REASONS = {
    "thesis_drift_invalidate",
    "TRIPWIRE_INVALIDATED",
    "archetype_ttl_expired",
    "archetype_level_conflict",
    "archetype_order_dead",
}


def _load_close_map(bars_dir: Path) -> Dict[str, Dict[str, float]]:
    close_map: Dict[str, Dict[str, float]] = {}
    for path in bars_dir.glob("US_*.json"):
        symbol = path.stem.replace("_", ".")
        bars = json.loads(path.read_text(encoding="utf-8"))
        close_map[symbol] = {
            str(bar[0]): float(bar[4])
            for bar in bars
            if len(bar) >= 5 and bar[0] and bar[4]
        }
    return close_map


def _gross_pnl(row: sqlite3.Row, close_map: Dict[str, Dict[str, float]]) -> float:
    close = close_map.get(row["symbol"], {}).get(row["date"])
    try:
        entry = float(row["entry_price"] or 0)
        size = float(row["position_size"] or 0)
    except (TypeError, ValueError):
        return 0.0
    if close is None or entry <= 0 or size <= 0:
        return 0.0
    direction = "BUY" if "多数方 BUY" in (row["skip_reason"] or "") else "SELL_SHORT"
    return (close - entry if direction == "BUY" else entry - close) * size


def _restored_direction(row: sqlite3.Row) -> str:
    return "BUY" if "多数方 BUY" in (row["skip_reason"] or "") else "SELL_SHORT"


def _majority_trim_count(buy_count: int, short_count: int) -> Tuple[str, int]:
    majority = "BUY" if buy_count > short_count else "SELL_SHORT"
    total = buy_count + short_count
    if total == 0 or max(buy_count, short_count) / total <= 0.8:
        return majority, 0
    for trim_count in range(1, total + 1):
        buys = buy_count - trim_count if majority == "BUY" else buy_count
        shorts = short_count - trim_count if majority == "SELL_SHORT" else short_count
        remaining = buys + shorts
        if remaining and max(buys, shorts) / remaining <= 0.8:
            return majority, trim_count
    return majority, total


def _ordered_victims(
    candidates: List[sqlite3.Row],
    trim_count: int,
    order: str,
    close_map: Dict[str, Dict[str, float]],
) -> List[sqlite3.Row]:
    if order == "reverse":
        ordered = list(reversed(candidates))
    elif order == "best_rescue":
        ordered = sorted(candidates, key=lambda row: _gross_pnl(row, close_map))
    elif order == "worst_rescue":
        ordered = sorted(
            candidates,
            key=lambda row: _gross_pnl(row, close_map),
            reverse=True,
        )
    elif order == "id":
        ordered = list(candidates)
    else:
        raise ValueError(f"unknown order: {order}")
    return ordered[:trim_count]


def replay(
    db_path: Path,
    bars_dir: Path,
    *,
    filter_terminal: bool,
    order: str = "id",
) -> Dict[str, object]:
    close_map = _load_close_map(bars_dir)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        dates = [
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT date FROM playbook_instructions "
                "WHERE date LIKE '2026-09-%' AND skip_reason LIKE '方向偏斜%' "
                "ORDER BY date"
            )
        ]
        all_cut: List[sqlite3.Row] = []
        still_cut: List[sqlite3.Row] = []
        rescued: List[sqlite3.Row] = []
        days: List[Dict[str, object]] = []

        for date in dates:
            rows = connection.execute(
                "SELECT id, date, symbol, direction, confidence, skip_reason, "
                "entry_price, position_size FROM playbook_instructions "
                "WHERE date = ? ORDER BY id",
                (date,),
            ).fetchall()
            active = [
                row for row in rows
                if row["direction"] in ("BUY", "SELL_SHORT")
                and (
                    not filter_terminal
                    or (row["skip_reason"] or "") not in TERMINAL_SKIP_REASONS
                )
            ]
            cut = [
                row for row in rows
                if (row["skip_reason"] or "").startswith("方向偏斜")
            ]
            directions = [row["direction"] for row in active]
            directions.extend(_restored_direction(row) for row in cut)
            counts = Counter(directions)
            buy_count = counts["BUY"]
            short_count = counts["SELL_SHORT"]
            majority, trim_count = _majority_trim_count(buy_count, short_count)
            majority_candidates = [
                row for row in cut if _restored_direction(row) == majority
            ]
            victims = _ordered_victims(
                majority_candidates, trim_count, order, close_map
            )
            victim_ids = {row["id"] for row in victims}
            day_rescued = [row for row in cut if row["id"] not in victim_ids]

            all_cut.extend(cut)
            still_cut.extend(victims)
            rescued.extend(day_rescued)
            days.append({
                "date": date,
                "active_prior": len(active),
                "actual_cut": len(cut),
                "union_buy": buy_count,
                "union_short": short_count,
                "day_trim": len(victims),
                "rescued": len(day_rescued),
                "rescued_gross_pnl": round(
                    sum(_gross_pnl(row, close_map) for row in day_rescued), 2
                ),
            })
    finally:
        connection.close()

    gross = sum(_gross_pnl(row, close_map) for row in rescued)
    return {
        "arm": "filter_terminal" if filter_terminal else "doc_unfiltered",
        "order": order,
        "actual_cut": len(all_cut),
        "still_cut": len(still_cut),
        "rescued": len(rescued),
        "rescued_gross_pnl": round(gross, 2),
        "days": days,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--bars", type=Path, default=DEFAULT_BARS)
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args()

    arms = [
        replay(args.db, args.bars, filter_terminal=False),
        replay(args.db, args.bars, filter_terminal=True),
    ]
    tie_orders = ["reverse", "best_rescue", "worst_rescue"]
    for tie_order in tie_orders:
        arms.append(
            replay(
                args.db,
                args.bars,
                filter_terminal=False,
                order=tie_order,
            )
        )

    if args.json:
        print(json.dumps(arms, ensure_ascii=False, indent=2))
        return 0

    print("arm                      order         cut still rescued gross_pnl")
    for result in arms:
        print(
            f"{result['arm']:<24} {result['order']:<13} "
            f"{result['actual_cut']:>3} {result['still_cut']:>5} "
            f"{result['rescued']:>7} {result['rescued_gross_pnl']:>10.2f}"
        )
    print(
        "Note: DB is post-hoc state; created_at/updated_at cannot reconstruct "
        "point-in-time guardrail vision."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
