#!/usr/bin/env python3
"""
Backtest: Config Change Validator — FTSeek-US P2 G15

Replays recent trade data against proposed parameter changes and outputs
before/after comparison. Answers: "Would this config change have helped?"

Usage:
  python scripts/backtest_config_change.py                   # default (30 days)
  python scripts/backtest_config_change.py --days 60         # 60-day window
  python scripts/backtest_config_change.py --dry-run         # show what WOULD change, no apply
  python scripts/backtest_config_change.py --apply           # apply learned alignments
  python scripts/backtest_config_change.py --report-only     # print stats, no changes
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("backtest_config")


def load_trades(days: int) -> list:
    """Load completed trades from DecisionRecorder."""
    from app.storage.decision_recorder import get_decision_recorder
    dr = get_decision_recorder()
    conn = dr._conn()
    try:
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        rows = conn.execute(
            "SELECT * FROM trade_outcomes "
            "WHERE outcome_correct IN (0, 1) AND created_at >= ? "
            "ORDER BY created_at DESC",
            (cutoff,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def compute_current_alignment(trades: list) -> Dict[str, Dict[str, Dict]]:
    """Compute win rate per (session, strategy) pair from actual trades."""
    pairs = {}
    for t in trades:
        sess = t.get("entry_session") or "unknown"
        strat = t.get("strategy_type") or "unknown"
        key = f"{sess}.{strat}"
        if key not in pairs:
            pairs[key] = {"correct": 0, "total": 0}
        pairs[key]["total"] += 1
        if t["outcome_correct"] == 1:
            pairs[key]["correct"] += 1

    result = {}
    for key, stats in pairs.items():
        parts = key.split(".", 1)
        sess, strat = parts[0], parts[1] if len(parts) > 1 else "unknown"
        wr = stats["correct"] / stats["total"] if stats["total"] > 0 else 0
        if sess not in result:
            result[sess] = {}
        result[sess][strat] = {"trades": stats["total"], "win_rate": round(wr, 3)}
    return result


def simulate_scoring(trades: list, new_alignment: Dict[str, Dict[str, float]]) -> Dict:
    """Simulate what composite scores would be with new alignment."""
    from app.pipeline.portfolio_constructor import SESSION_STRATEGY_ALIGNMENT, SCORE_WEIGHTS

    results = {"match_improved": 0, "match_worsened": 0, "match_same": 0, "total": 0}
    for t in trades:
        sess = t.get("entry_session", "")
        strat = t.get("strategy_type", "")
        if not sess or not strat:
            continue

        old_align = SESSION_STRATEGY_ALIGNMENT.get(sess, {}).get(strat, 0.70)
        new_align = new_alignment.get(sess, {}).get(strat, old_align)
        results["total"] += 1

        delta = (new_align - old_align) * SCORE_WEIGHTS["session"]
        if delta > 0.01:
            results["match_improved"] += 1
        elif delta < -0.01:
            results["match_worsened"] += 1
        else:
            results["match_same"] += 1

    return results


def main():
    parser = argparse.ArgumentParser(description="Backtest config changes against trade history")
    parser.add_argument("--days", type=int, default=30, help="Lookback window (default: 30)")
    parser.add_argument("--dry-run", action="store_true", help="Show proposed changes without applying")
    parser.add_argument("--apply", action="store_true", help="Apply learned alignments to live config")
    parser.add_argument("--blend", type=float, default=0.5, help="Blend ratio for applying (default: 0.5)")
    parser.add_argument("--report-only", action="store_true", help="Only print current alignment stats")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    trades = load_trades(args.days)
    if not trades:
        print(f"No completed trades found in the last {args.days} days.")
        return

    current = compute_current_alignment(trades)

    if args.report_only:
        if args.json:
            print(json.dumps(current, indent=2))
        else:
            print(f"\n=== Current Win Rate by (Session, Strategy) — {len(trades)} trades over {args.days}d ===\n")
            for sess in sorted(current):
                for strat, stats in sorted(current[sess].items()):
                    bar = "█" * int(stats["win_rate"] * 20) + "░" * (20 - int(stats["win_rate"] * 20))
                    print(f"  {sess:<16} {strat:<18} {bar} {stats['win_rate']:.0%} ({stats['trades']} trades)")
        return

    # Build proposed alignment from actual win rates
    from app.pipeline.portfolio_constructor import SESSION_STRATEGY_ALIGNMENT
    proposed = {}
    for sess, strategies in current.items():
        proposed[sess] = {}
        for strat, stats in strategies.items():
            proposed[sess][strat] = round(0.4 + stats["win_rate"] * 0.6, 2)

    # Show before/after
    if args.json:
        output = {
            "window_days": args.days,
            "total_trades": len(trades),
            "current_alignment": SESSION_STRATEGY_ALIGNMENT,
            "proposed_alignment": proposed,
            "per_pair_stats": current,
        }
        scoring = simulate_scoring(trades, proposed)
        output["score_impact"] = scoring
        print(json.dumps(output, indent=2))
    else:
        print(f"\n{'='*70}")
        print(f"  Config Change Backtest — {len(trades)} trades over {args.days} days")
        print(f"{'='*70}\n")
        print(f"{'Session':<16} {'Strategy':<18} {'Prior':>7} {'→':>3} {'Proposed':>8} {'WinRate':>8} {'Trades':>7}")
        print(f"{'-'*16} {'-'*18} {'-'*7} {'-'*3} {'-'*8} {'-'*8} {'-'*7}")

        diffs = 0
        for sess in sorted(proposed):
            for strat in sorted(proposed[sess]):
                prior = SESSION_STRATEGY_ALIGNMENT.get(sess, {}).get(strat, 0.70)
                new = proposed[sess][strat]
                wr = current.get(sess, {}).get(strat, {}).get("win_rate", 0)
                n = current.get(sess, {}).get(strat, {}).get("trades", 0)
                marker = " *" if abs(new - prior) > 0.02 else ""
                print(f"  {sess:<16} {strat:<18} {prior:>6.2f}  →  {new:<7.2f} {wr:>7.0%} {n:>6}{marker}")
                if abs(new - prior) > 0.01:
                    diffs += 1

        print(f"\n  {diffs} alignment changes proposed.")

        scoring = simulate_scoring(trades, proposed)
        print(f"\n  Score impact: {scoring['match_improved']} improved, "
              f"{scoring['match_worsened']} worsened, {scoring['match_same']} unchanged "
              f"(out of {scoring['total']})")

    # Apply
    if args.apply:
        from app.pipeline.portfolio_constructor import PortfolioConstructor
        PortfolioConstructor.apply_learned_alignment(proposed, blend=args.blend)
        print(f"\n  Applied with blend={args.blend:.0%}. New alignment:")
        for sess in sorted(SESSION_STRATEGY_ALIGNMENT):
            for strat, val in sorted(SESSION_STRATEGY_ALIGNMENT[sess].items()):
                print(f"    {sess}.{strat} = {val:.2f}")
    elif args.dry_run:
        print("\n  [DRY RUN] No changes applied. Use --apply to apply proposed alignments.")


if __name__ == "__main__":
    main()
