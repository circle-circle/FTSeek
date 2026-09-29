#!/usr/bin/env python3
"""Offline runner counterfactual for SL_TRAIL_LOCK exits (record-only).

Inputs are already-collected artifacts only:
  - data/shadow/post_exit_path.jsonl (parent exit + status)
  - data/ftseek_us.db (quantity, ATR metadata)
  - data/replay_bars/<SYMBOL>_<DATE>.json (local 1-minute bars)

The counterfactual keeps a fraction of the parent position open at the parent
exit price and trails it by k*ATR. It never fetches bars and never changes any
trading behavior. Paired gross/net delta is the same because the main and runner
legs replace the original full close and both eventually close; fee-linearity is
declared in the report rather than silently assumed to be exact broker pricing.
"""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

DB = REPO / "data" / "ftseek_us.db"
POST_EXIT = REPO / "data" / "shadow" / "post_exit_path.jsonl"
BAR_DIR = REPO / "data" / "replay_bars"
OUT = REPO / "data" / "shadow" / "trail_lock_runner_cf.jsonl"
OUT_LATEST = REPO / "data" / "shadow" / "trail_lock_runner_cf_latest.json"

RUNNER_FRACTIONS = (0.25, 0.30, 0.40)
ATR_MULTIPLIERS = (0.25, 0.30, 0.35)
MIN_N = 30
BOOTSTRAP_ITERATIONS = 10_000
BOOTSTRAP_SEED = 42


def _load_post_exit_rows(path=POST_EXIT):
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (row.get("type") == "post_exit" and row.get("status") == "ok"
                and row.get("exit_reason") == "SL_TRAIL_LOCK"):
            rows.append(row)
    return rows


def _load_trade_details(trade_ids):
    if not trade_ids:
        return {}
    placeholders = ",".join("?" for _ in trade_ids)
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, symbol, direction, quantity, metadata_json "
            f"FROM trade_records WHERE id IN ({placeholders})", tuple(trade_ids)
        ).fetchall()
    finally:
        conn.close()
    result = {}
    for row in rows:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except json.JSONDecodeError:
            metadata = {}
        result[int(row["id"])] = {
            "symbol": row["symbol"], "direction": row["direction"],
            "quantity": float(row["quantity"] or 0), "metadata": metadata,
        }
    return result


def _load_bars(symbol, et_date):
    path = BAR_DIR / f"{symbol.replace('.', '_')}_{et_date}.json"
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


def _bars_after(bars, exit_min):
    return [bar for bar in bars if str(bar[0])[11:16] > str(exit_min)]


def settle_runner_leg(direction, parent_exit, atr_abs, bars, atr_multiplier):
    """Trail one runner leg from the parent exit through local 1-minute bars."""
    direction = str(direction or "").upper()
    if direction not in ("LONG", "SHORT") or parent_exit <= 0 or atr_abs <= 0:
        return {}
    trail_distance = float(atr_abs) * float(atr_multiplier)
    if trail_distance <= 0 or not bars:
        return {}

    exit_price = None
    exit_reason = None
    if direction == "LONG":
        peak = float(parent_exit)
        stop = peak - trail_distance
        for bar in bars:
            low = float(bar[3])
            high = float(bar[2])
            if low <= stop:
                exit_price, exit_reason = stop, "runner_stop"
                break
            peak = max(peak, high)
            stop = max(stop, peak - trail_distance)
    else:
        trough = float(parent_exit)
        stop = trough + trail_distance
        for bar in bars:
            high = float(bar[2])
            low = float(bar[3])
            if high >= stop:
                exit_price, exit_reason = stop, "runner_stop"
                break
            trough = min(trough, low)
            stop = min(stop, trough + trail_distance)

    if exit_price is None:
        exit_price, exit_reason = float(bars[-1][4]), "eod_close"
    return {
        "exit_price": round(float(exit_price), 6),
        "exit_reason": exit_reason,
        "n_bars": len(bars),
    }


def _signed_price_delta(direction, parent_exit, runner_exit):
    if direction == "LONG":
        return runner_exit - parent_exit
    return parent_exit - runner_exit


def _bootstrap_ci(values, iterations=BOOTSTRAP_ITERATIONS, seed=BOOTSTRAP_SEED):
    if not values:
        return None
    rng = random.Random(seed)
    means = []
    for _ in range(iterations):
        sample = rng.choices(values, k=len(values))
        means.append(statistics.mean(sample))
    means.sort()
    lower = means[int(0.025 * (len(means) - 1))]
    upper = means[int(0.975 * (len(means) - 1))]
    return [round(lower, 2), round(upper, 2)]


def summarize(records, iterations=BOOTSTRAP_ITERATIONS):
    grouped = defaultdict(list)
    for record in records:
        if record.get("status") != "ok":
            continue
        grouped[(record["runner_fraction"], record["atr_multiplier"])].append(
            float(record["delta_usd"]))
    result = {}
    for (fraction, multiplier), values in sorted(grouped.items()):
        median = statistics.median(values)
        confidence_interval = _bootstrap_ci(values, iterations=iterations)
        result[f"fraction={fraction:.2f}/atr_mult={multiplier:.2f}"] = {
            "n": len(values),
            "sum_delta_usd": round(sum(values), 2),
            "mean_delta_usd": round(statistics.mean(values), 2),
            "median_delta_usd": round(median, 2),
            "win_rate": round(sum(1 for value in values if value > 0) / len(values), 3),
            "bootstrap_95_ci_usd": confidence_interval,
            "proposal_gate": bool(
                len(values) >= MIN_N and median > 0
                and confidence_interval is not None and confidence_interval[0] > 0
            ),
        }
    return result


def _atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description="Offline TRAIL_LOCK runner counterfactual")
    parser.add_argument("--iterations", type=int, default=BOOTSTRAP_ITERATIONS)
    args = parser.parse_args()

    post_rows = _load_post_exit_rows()
    details = _load_trade_details([row["trade_id"] for row in post_rows])
    records = []
    skipped = {"missing_trade": 0, "missing_atr": 0, "missing_bars": 0}
    for row in post_rows:
        trade_id = row.get("trade_id")
        detail = details.get(trade_id)
        if not detail:
            skipped["missing_trade"] += 1
            continue
        direction = str(detail.get("direction") or row.get("direction") or "").upper()
        quantity = float(detail.get("quantity") or 0)
        parent_exit = float(row.get("exit_px") or 0)
        metadata = detail.get("metadata") or {}
        atr_abs = float(metadata.get("atr") or 0)
        bars = _bars_after(_load_bars(row.get("symbol"), row.get("date")), row.get("exit_min"))
        if atr_abs <= 0:
            skipped["missing_atr"] += 1
        if not bars:
            skipped["missing_bars"] += 1
        for fraction in RUNNER_FRACTIONS:
            for multiplier in ATR_MULTIPLIERS:
                base = {
                    "type": "runner_cf", "trade_id": trade_id, "symbol": row.get("symbol"),
                    "direction": direction, "date": row.get("date"),
                    "exit_min": row.get("exit_min"), "parent_exit": round(parent_exit, 6),
                    "quantity": quantity, "runner_fraction": fraction,
                    "atr_multiplier": multiplier,
                }
                if quantity <= 0 or parent_exit <= 0:
                    records.append({**base, "status": "bad_trade"})
                    continue
                if atr_abs <= 0:
                    records.append({**base, "status": "missing_atr"})
                    continue
                if not bars:
                    records.append({**base, "status": "missing_bars"})
                    continue
                leg = settle_runner_leg(direction, parent_exit, atr_abs, bars, multiplier)
                if not leg:
                    records.append({**base, "status": "missing_bars"})
                    continue
                signed_delta = _signed_price_delta(direction, parent_exit, leg["exit_price"])
                delta_usd = signed_delta * quantity * fraction
                records.append({
                    **base, **leg, "status": "ok",
                    "runner_return_pct": round(signed_delta / parent_exit * 100, 4),
                    "gross_delta_usd": round(delta_usd, 2),
                    "net_delta_usd": round(delta_usd, 2),
                    "delta_usd": round(delta_usd, 2),
                })

    summary = summarize(records, iterations=args.iterations)
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "n_parent_exits": len(post_rows), "n_rows": len(records),
        "skipped_parent_exits": skipped, "variants": summary,
        "behavior_change": False,
        "net_delta_note": "gross=net by fee-neutral paired-close approximation; enforce requires a separate ledger design",
        "proposal_gate": {
            "min_n": MIN_N,
            "criteria": "n>=30 and paired median delta>0 and bootstrap 95% CI lower bound>0",
            "next_step": "user adjudication only; no automatic behavior change",
        },
    }
    _atomic_write(OUT, "\n".join(json.dumps(row, ensure_ascii=False) for row in records) + "\n")
    _atomic_write(OUT_LATEST, json.dumps(report, ensure_ascii=False, indent=1))
    print(f"[runner-cf] parent exits={len(post_rows)} rows={len(records)} "
          f"skipped_parent_exits={skipped}")
    for name, values in summary.items():
        print(f"  {name}: n={values['n']} sum=${values['sum_delta_usd']} "
              f"median=${values['median_delta_usd']} CI={values['bootstrap_95_ci_usd']} "
              f"gate={values['proposal_gate']}")
    print(f"→ {OUT_LATEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
