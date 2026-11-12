#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from app.playbook.pilot_policy import (  # noqa: E402
    MOM_ETF_V1,
    compute_mom_candidates,
    previous_local_trading_day,
    rebalance_day_status,
    mom_etf_v1_decision_id,
    size_mom_order,
)


def load_bars() -> Dict[str, List[Any]]:
    bars_by_symbol: Dict[str, List[Any]] = {}
    for symbol in MOM_ETF_V1.universe:
        path = REPO / "data" / "replay_bars_daily" / f"{symbol.replace('.', '_')}.json"
        if not path.exists():
            raise FileNotFoundError(f"missing daily bars: {path}")
        rows = json.loads(path.read_text(encoding="utf-8"))
        rows.sort(key=lambda row: str(row[0])[:10])
        bars_by_symbol[symbol] = rows
    return bars_by_symbol


def build_instruction(candidate: Dict[str, Any], trade_date: str) -> Dict[str, Any]:
    from app.execution.fee_model import estimate_round_trip

    symbol = candidate["symbol"]
    close = float(candidate["close"])
    decision_id = mom_etf_v1_decision_id(trade_date, symbol)
    sizing = size_mom_order(
        close,
        lambda quantity, price: estimate_round_trip(quantity, price, price, "LONG", trade_date)["total"],
    )
    quantity = int(sizing["quantity"]) if sizing else 0
    stop_loss = close * (1.0 - MOM_ETF_V1.initial_sl_pct / 100.0)
    return {
        "date": trade_date,
        "symbol": symbol,
        "direction": "BUY",
        "decision_id": decision_id,
        "confidence": 100,
        "strength": round(float(candidate["momentum_12_1"]) * 100.0, 4),
        "entry_price": close,
        "stop_loss": stop_loss,
        "take_profit": 0.0,
        "position_size": quantity,
        "horizon": "long",
        "horizon_reasoning": "MOM-ETF-V1 fixed 21 trading-day plumbing pilot",
        "triggered": 1,
        "raw_instruction": {
            "source": "mom_etf_v1",
            "policy_id": MOM_ETF_V1.policy_id,
            "universe_version": MOM_ETF_V1.universe_version,
            "asof_date": candidate["asof_date"],
            "momentum_12_1": candidate["momentum_12_1"],
            "cash_filter": MOM_ETF_V1.cash_filter,
            "candidate_screening": {
                "candidate_id": decision_id,
                "source": "mom_etf_v1",
            },
            "sleeve": "MOM-ETF-V1",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="MOM-ETF-V1 SIM instruction writer")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="bypass rebalance-day gate for local rehearsal only")
    args = parser.parse_args()

    from app.utils.timezone import now_et

    now_et_dt = now_et()
    trade_date = now_et_dt.date().isoformat()
    if not args.force:
        rebalance = rebalance_day_status(now_et_dt.date())
        if rebalance.reason == "calendar_unavailable":
            print(json.dumps({"status": "fail_closed", "reason": "calendar_unavailable",
                              "date": trade_date, "error": str(rebalance.error)}))
            return 1
        if not rebalance.is_rebalance_day:
            print(json.dumps({"status": "no_op", "reason": rebalance.reason,
                              "date": trade_date,
                              "trading_day_index": rebalance.trading_day_index}))
            return 0

    try:
        expected_asof_date = previous_local_trading_day(now_et_dt.date()).isoformat()
    except Exception as exc:
        print(json.dumps({"status": "fail_closed", "reason": "previous_trading_day_unavailable",
                          "date": trade_date, "error": str(exc)}))
        return 1

    bars_by_symbol = load_bars()
    asof_date = str(bars_by_symbol["US.QQQ"][-1][0])[:10]
    if asof_date != expected_asof_date:
        print(json.dumps({"status": "fail_closed", "reason": "bars_not_previous_trading_day",
                          "asof": asof_date, "expected_asof": expected_asof_date}))
        return 1
    for symbol, bars in bars_by_symbol.items():
        if str(bars[-1][0])[:10] != asof_date:
            print(json.dumps({"status": "fail_closed", "reason": "asof_mismatch", "symbol": symbol}))
            return 1

    candidates = compute_mom_candidates(bars_by_symbol, asof_date)
    if not candidates:
        print(json.dumps({"status": "cash", "reason": "qqq_below_ma200_or_no_data", "asof": asof_date}))
        return 0

    instructions = [build_instruction(candidate, trade_date) for candidate in candidates]
    if args.dry_run:
        print(json.dumps({"status": "dry_run", "date": trade_date, "asof": asof_date,
                          "instructions": instructions}, ensure_ascii=False, indent=2))
        return 0

    from app.playbook.db import route_playbook_db
    from app.playbook.ue_batch import confirm_saved

    db = route_playbook_db("sim")
    results = []
    for instruction in instructions:
        try:
            instruction_id = db.save_instruction(instruction)
            verdict = confirm_saved(db, instruction, instruction_id)
        except Exception as exc:
            instruction_id = None
            verdict = "unknown"
            print(json.dumps({"status": "unknown", "reason": str(exc),
                              "decision_id": instruction.get("decision_id")}), file=sys.stderr)
        row = db.get_instruction_by_id(instruction_id) if instruction_id is not None else None
        results.append({
            "id": instruction_id,
            "status": verdict,
            "decision_id": (row or {}).get("decision_id"),
            "symbol": (row or {}).get("symbol"),
            "direction": (row or {}).get("direction"),
            "skip_reason": (row or {}).get("skip_reason"),
        })
    saved = [row for row in results if row["status"] == "saved"]
    rejected = [row for row in results if row["status"] == "rejected"]
    unknown = [row for row in results if row["status"] == "unknown"]
    status = "saved" if len(saved) == len(results) else ("rejected" if rejected else "unknown")
    print(json.dumps({"status": status, "date": trade_date, "asof": asof_date,
                      "saved": saved, "rejected": rejected, "unknown": unknown},
                     ensure_ascii=False, indent=2))
    return 0 if status == "saved" else 1


if __name__ == "__main__":
    raise SystemExit(main())
