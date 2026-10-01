#!/usr/bin/env python3
"""Backfill playbook_instructions exit fields from closed trade_records rows.

Ground truth (2026-10-01 audit): 2 rows (pi.id 2550 US.UBER / 2771 US.NOK)
were closed on 9/17 by the premarket exit path using temporary thresholds
(tighten/lock_in, retired by user errata 9/18) WITHOUT instruction_id, so
the `if inst_id:` guard in _book_playbook_exit_result never fired —
playbook_instructions kept outcome='holding', exit_reason NULL and a stale
unrealized pnl snapshot (NOK: pi -112.13 vs authoritative net +108.43,
sign flip). The current exit chain books via _book_playbook_exit_result with
instruction_id, so no new gaps appear; this is a one-off historical repair.

Repair semantics:
  - source of truth = trade_records (authoritative fill ledger):
      realized_pnl (gross) - fees_est (fee) = net pnl
      (matches the 2026-08-20 net-semantics switch for playbook pnl)
  - only touches rows where the trade is CLOSED (exit_time & exit_price set)
    and pi.outcome is still holding/already_held/NULL (never win/loss/void —
    those are terminal states, same idempotence guard as pipeline settle)
  - append-only JSONL audit of the before-image; dry-run by default
  - idempotent: re-running finds 0 rows after --apply
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "ftseek_us.db"
DEFAULT_AUDIT_DIR = ROOT / "data" / "audits"

# pi.outcome states that mean "not settled yet" (terminal states never touched)
_UNSETTLED = ("holding", "already_held", "", None)


def find_gaps(db_path: Path) -> List[Dict[str, Any]]:
    """Closed trade_records whose linked instruction is still unsettled."""
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT t.id AS trade_id, t.symbol, t.direction, t.entry_price, "
            "       t.exit_price, t.exit_time, t.exit_reason, t.realized_pnl, "
            "       t.fees_est, t.holding_hours, t.quantity, t.metadata_json, "
            "       p.id AS instruction_id, p.outcome, p.pnl, p.exit_reason AS pi_exit_reason "
            "FROM trade_records t "
            "JOIN playbook_instructions p "
            "  ON p.id = CAST(json_extract(t.metadata_json, '$.playbook_instruction_id') AS INTEGER) "
            "WHERE t.source_type = 'PLAYBOOK_SIM' "
            "  AND t.realized_pnl IS NOT NULL "
            "  AND COALESCE(t.exit_time, '') != '' "
            "  AND COALESCE(t.exit_price, 0) > 0 "
            # unsettled only: holding/NULL. Legacy 'closed'/'already_held' are
            # terminal states of older schemas — out of audit scope, untouched.
            "  AND (p.outcome IS NULL OR p.outcome = 'holding') "
            # ghost/orphan closes book as outcome='void' per the E2 contract
            # (pipeline settle, 2026-07-19) — never backfill them as win/loss.
            "  AND COALESCE(t.exit_reason, '') NOT LIKE '%GHOST%' "
            "  AND COALESCE(t.exit_reason, '') NOT LIKE '%ORPHAN%' "
            "  AND COALESCE(t.exit_reason, '') NOT LIKE '%PHANTOM%' "
            "ORDER BY p.id"
        ).fetchall()
        out: List[Dict[str, Any]] = []
        for row in rows:
            d = dict(row)
            gross = float(d.get("realized_pnl") or 0.0)
            fee = float(d.get("fees_est") or 0.0)
            net = round(gross - fee, 2)
            entry = float(d.get("entry_price") or 0.0)
            qty = float(d.get("quantity") or 0.0)
            if qty <= 0:
                qty_meta = json.loads(d.get("metadata_json") or "{}")
                qty = float(qty_meta.get("quantity") or 0.0)
            notional = entry * qty
            net_pct = round(net / notional, 6) if notional > 0 else None
            d["backfill"] = {
                "outcome": "win" if net > 0 else "loss",
                "exit_price": d.get("exit_price"),
                "exit_time": d.get("exit_time"),
                "exit_reason": d.get("exit_reason") or "external_close",
                "pnl": net,
                "pnl_pct": net_pct,
                "holding_period_hours": d.get("holding_hours"),
            }
            out.append(d)
        return out
    finally:
        connection.close()


def apply_backfill(
    db_path: Path,
    gaps: List[Dict[str, Any]],
    audit_path: Path,
) -> None:
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_path.open("w", encoding="utf-8") as audit_file:
        for row in gaps:
            audit_file.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    if not gaps:
        return
    connection = sqlite3.connect(db_path)
    try:
        with connection:
            for row in gaps:
                bf = row["backfill"]
                # Same terminal-state guard as the live settle path, narrowed
                # to the audit scope (holding/NULL only) for race safety.
                connection.execute(
                    "UPDATE playbook_instructions SET outcome=?, exit_price=?, "
                    "exit_time=?, exit_reason=?, pnl=?, pnl_pct=?, holding_period_hours=? "
                    "WHERE id=? AND (outcome IS NULL OR outcome = 'holding')",
                    (bf["outcome"], bf["exit_price"], bf["exit_time"], bf["exit_reason"],
                     bf["pnl"], bf["pnl_pct"], bf["holding_period_hours"],
                     row["instruction_id"]),
                )
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--apply", action="store_true",
                        help="write backfill (default: dry-run report only)")
    parser.add_argument("--audit-dir", type=Path, default=DEFAULT_AUDIT_DIR)
    args = parser.parse_args()

    gaps = find_gaps(args.db)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    audit_path = args.audit_dir / f"instruction_exit_backfill_{stamp}.jsonl"
    mode = "apply" if args.apply else "dry-run"
    print(f"mode={mode} gap_count={len(gaps)} audit_file={audit_path}")
    for g in gaps:
        bf = g["backfill"]
        print(f"  pi.id={g['instruction_id']} {g['symbol']:<9} "
              f"outcome {g['outcome']}→{bf['outcome']} pnl {g['pnl']}→{bf['pnl']} "
              f"exit={bf['exit_reason']} @{str(bf['exit_time'])[:19]}")
    if args.apply:
        apply_backfill(args.db, gaps, audit_path)
        remaining = find_gaps(args.db)
        print(f"applied={len(gaps)} recheck_remaining={len(remaining)}")
        return 0 if not remaining else 1
    print("dry-run: no DB changes; pass --apply to backfill")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
