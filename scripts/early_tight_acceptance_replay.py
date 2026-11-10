#!/usr/bin/env python3
"""Fixed 120s early-tight acceptance replay; read-only and exploratory.

The population and decision rules are frozen in
docs/debates/DEB-20261105-sl-initial-early-tight-acceptance.md.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "ftseek_us.db"
BAR_DIR = ROOT / "data" / "replay_bars"
OUT = ROOT / "data" / "reports" / "early_tight_acceptance_replay_20261105.json"
PENETRATION = ROOT / "data" / "shadow" / "penetration_shadow.jsonl"
GRACE_BARS = 2
MAX_EFFECTIVE_GRACE_SEC = 180
MIN_EVAL_N = 30
MIN_EVAL_DAYS = 10
Z80 = 1.2815515655446004


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(str(value)).replace(tzinfo=None)


def _signed_gross(direction: str, entry: float, exit_price: float, quantity: float) -> float:
    sign = 1.0 if direction == "LONG" else -1.0
    return sign * (float(exit_price) - float(entry)) * float(quantity)


def _signed_bps(direction: str, entry: float, reference: float, candidate: float) -> float:
    sign = 1.0 if direction == "LONG" else -1.0
    return sign * (float(candidate) - float(reference)) / float(entry) * 10000.0


def _stop_hit(direction: str, initial_sl: float, bar: list) -> bool:
    return (direction == "LONG" and float(bar[3]) <= initial_sl) or (
        direction == "SHORT" and float(bar[2]) >= initial_sl)


def _close_recovered(direction: str, tight_sl: float, bar: list) -> bool:
    return (direction == "LONG" and float(bar[4]) >= tight_sl) or (
        direction == "SHORT" and float(bar[4]) <= tight_sl)


def _stop_fill(direction: str, initial_sl: float, bar: list) -> float:
    if direction == "LONG":
        return min(float(initial_sl), float(bar[1]))
    return max(float(initial_sl), float(bar[1]))


def _containing_bar_end(exit_time: datetime) -> datetime:
    floor = exit_time.replace(second=0, microsecond=0)
    if exit_time == floor:
        return floor
    return floor + timedelta(minutes=1)


def _trigger_geometry(trade: dict[str, Any], bars: list) -> dict[str, Any]:
    direction = trade["direction"]
    tight = float(trade["tight_sl"])
    initial = float(trade["initial_sl"])
    containing = _containing_bar_end(_dt(trade["exit_time"]))
    nearby = [b for b in bars if containing - timedelta(minutes=1) <= _dt(b[0]) <= containing]
    visible = any(
        (direction == "LONG" and float(b[3]) <= tight)
        or (direction == "SHORT" and float(b[2]) >= tight)
        for b in nearby
    )
    containing_rows = [b for b in nearby if _dt(b[0]) == containing]
    hard_overlap = any(_stop_hit(direction, initial, b) for b in containing_rows)
    wider = (direction == "LONG" and initial < tight) or (
        direction == "SHORT" and initial > tight)
    return {
        "trigger_visible": visible,
        "trigger_bar_hard_overlap": hard_overlap,
        "valid_stop_geometry": wider,
        "containing_bar_end": containing.isoformat(sep=" "),
    }


def evaluate_trade(trade: dict[str, Any], bars: list, optimistic: bool = False) -> dict[str, Any]:
    direction = trade["direction"]
    entry = float(trade["entry_price"])
    initial = float(trade["initial_sl"])
    tight = float(trade["tight_sl"])
    quantity = float(trade["quantity"])
    fees = float(trade.get("fees_est") or 0.0)
    geometry = _trigger_geometry(trade, bars)
    base = {
        "trade_id": trade["trade_id"],
        "symbol": trade["symbol"],
        "direction": direction,
        "date": trade["entry_time"][:10],
        "entry_time": trade["entry_time"],
        "exit_time": trade["exit_time"],
        "entry_price": entry,
        "actual_exit_price": float(trade["actual_exit_price"]),
        "tight_sl": tight,
        "initial_sl": initial,
        **geometry,
    }
    if not geometry["valid_stop_geometry"]:
        return {**base, "status": "invalid_stop_geometry"}
    if not geometry["trigger_visible"]:
        return {**base, "status": "trigger_not_visible"}
    if geometry["trigger_bar_hard_overlap"]:
        return {**base, "status": "trigger_bar_hard_overlap"}

    containing = _containing_bar_end(_dt(trade["exit_time"]))
    after = sorted((b for b in bars if _dt(b[0]) > containing), key=lambda b: _dt(b[0]))
    if len(after) < GRACE_BARS:
        return {**base, "status": "insufficient_grace_bars"}
    grace = after[:GRACE_BARS]
    if any((_dt(grace[i][0]) - _dt(grace[i - 1][0])).total_seconds() != 60
           for i in range(1, len(grace))):
        return {**base, "status": "nonconsecutive_grace_bars"}
    effective_grace_sec = int((_dt(grace[-1][0]) - _dt(trade["exit_time"])).total_seconds())
    if effective_grace_sec > MAX_EFFECTIVE_GRACE_SEC:
        return {**base, "status": "grace_timing_out_of_bounds"}

    recovered_at = None
    candidate_price = None
    candidate_reason = None
    ambiguous_same_bar = False
    for index, bar in enumerate(grace):
        hard = _stop_hit(direction, initial, bar)
        recovered = _close_recovered(direction, tight, bar)
        if hard and recovered:
            ambiguous_same_bar = True
        if hard and (not recovered or not optimistic):
            candidate_price = _stop_fill(direction, initial, bar)
            candidate_reason = "ORIGINAL_SL_DURING_GRACE"
            break
        if recovered:
            recovered_at = index + 1
            break
    if candidate_reason is None and recovered_at is None:
        candidate_price = float(grace[-1][4])
        candidate_reason = "GRACE_EXPIRED"

    if recovered_at is not None:
        remaining = after[recovered_at:]
        for bar in remaining:
            if _stop_hit(direction, initial, bar):
                candidate_price = _stop_fill(direction, initial, bar)
                candidate_reason = "ORIGINAL_SL_AFTER_RECOVERY"
                break
        if candidate_reason is None:
            if not remaining or _dt(after[-1][0]).strftime("%H:%M") < "15:55":
                return {**base, "status": "incomplete_eod_path",
                        "recovered_at_bar": recovered_at,
                        "effective_grace_sec": effective_grace_sec}
            candidate_price = float(after[-1][4])
            candidate_reason = "EOD_AFTER_RECOVERY"

    actual_gross = float(trade["actual_gross"])
    raw_actual_net = trade.get("actual_net")
    actual_net = float(raw_actual_net) if raw_actual_net is not None else actual_gross - fees
    if not math.isfinite(actual_net):
        actual_net = actual_gross - fees
    candidate_gross = _signed_gross(direction, entry, candidate_price, quantity)
    candidate_net = candidate_gross - fees
    delta_net = candidate_net - actual_net
    return {
        **base,
        "status": "ok",
        "optimistic": bool(optimistic),
        "effective_grace_sec": effective_grace_sec,
        "recovered_at_bar": recovered_at,
        "close_accepted_120s": recovered_at is not None,
        "ambiguous_same_bar": ambiguous_same_bar,
        "candidate_reason": candidate_reason,
        "candidate_exit_price": round(float(candidate_price), 6),
        "actual_gross": round(actual_gross, 4),
        "fees_est": round(fees, 4),
        "actual_net": round(actual_net, 4),
        "candidate_gross": round(candidate_gross, 4),
        "candidate_net": round(candidate_net, 4),
        "delta_net": round(delta_net, 4),
        "delta_bps": round(_signed_bps(
            direction, entry, float(trade["actual_exit_price"]), candidate_price), 4),
    }


def _cluster(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_day: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        by_day[row["date"]].append(float(row["delta_net"]))
    day_means = [statistics.mean(values) for _, values in sorted(by_day.items())]
    n_days = len(day_means)
    mean = statistics.mean(day_means) if day_means else None
    mde = None
    ci = None
    if n_days >= 2:
        mde = Z80 * statistics.stdev(day_means) / math.sqrt(n_days)
        ci = [mean - mde, mean + mde]
    return {
        "n_days": n_days,
        "day_mean_delta_net": round(mean, 4) if mean is not None else None,
        "mde80": round(mde, 4) if mde is not None else None,
        "ci80": [round(x, 4) for x in ci] if ci else None,
        "daily_totals": {day: round(sum(values), 4) for day, values in sorted(by_day.items())},
    }


def _window_split(rows: list[dict[str, Any]]) -> dict[str, Any]:
    dates = sorted({row["date"] for row in rows})
    if len(dates) < 2:
        return {"first_half": None, "second_half": None}
    midpoint = len(dates) // 2
    halves = (dates[:midpoint], dates[midpoint:])
    out = {}
    for name, days in zip(("first_half", "second_half"), halves):
        selected = [row for row in rows if row["date"] in days]
        out[name] = {
            "n": len(selected),
            "days": len(days),
            "delta_net": round(sum(row["delta_net"] for row in selected), 4),
        }
    return out


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [row for row in rows if row.get("status") == "ok"]
    reasons = Counter(row["candidate_reason"] for row in valid)
    cluster = _cluster(valid)
    total = sum(row["delta_net"] for row in valid)
    daily = cluster["daily_totals"]
    largest_day = max(daily, key=daily.get) if daily else None
    without_largest = total - (daily.get(largest_day) or 0.0)
    enough = len(valid) >= MIN_EVAL_N and cluster["n_days"] >= MIN_EVAL_DAYS
    verdict = "EXPLORATORY"
    if enough:
        lower = (cluster.get("ci80") or [None])[0]
        halves = _window_split(valid)
        half_values = [halves[name]["delta_net"] for name in ("first_half", "second_half")
                       if halves.get(name)]
        if (lower is not None and lower > 0 and total > 0 and without_largest >= 0
                and len(half_values) == 2 and all(value > 0 for value in half_values)):
            verdict = "SHADOW_PASS_FOR_RFC_ONLY"
        else:
            verdict = "NO_PROPOSAL"
    return {
        "n": len(valid),
        "recovered_n": sum(bool(row["close_accepted_120s"]) for row in valid),
        "candidate_reasons": dict(reasons),
        "actual_net": round(sum(row["actual_net"] for row in valid), 4),
        "candidate_net": round(sum(row["candidate_net"] for row in valid), 4),
        "delta_net": round(total, 4),
        "delta_net_mean": round(statistics.mean([row["delta_net"] for row in valid]), 4) if valid else None,
        "delta_net_median": round(statistics.median([row["delta_net"] for row in valid]), 4) if valid else None,
        "delta_bps": round(sum(row["delta_bps"] for row in valid), 4),
        "day_clustered": cluster,
        "window_split": _window_split(valid),
        "largest_positive_day": largest_day,
        "delta_without_largest_day": round(without_largest, 4),
        "ambiguous_same_bar_n": sum(bool(row["ambiguous_same_bar"]) for row in valid),
        "verdict": verdict,
    }


def load_cohort(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    sql = """SELECT id, symbol, direction, entry_time, exit_time, entry_price, exit_price,
                    quantity, realized_pnl, pnl_net, fees_est, metadata_json
             FROM trade_records
             WHERE source_type='PLAYBOOK_SIM' AND exit_reason='SL_INITIAL'
               AND exit_time IS NOT NULL ORDER BY entry_time"""
    cohort = []
    for row in conn.execute(sql):
        try:
            meta = json.loads(row["metadata_json"] or "{}")
            if not meta.get("_tight_sl_killed"):
                continue
            cohort.append({
                "trade_id": row["id"], "symbol": row["symbol"], "direction": row["direction"],
                "entry_time": row["entry_time"], "exit_time": row["exit_time"],
                "entry_price": row["entry_price"], "actual_exit_price": row["exit_price"],
                "quantity": row["quantity"], "actual_gross": row["realized_pnl"],
                "actual_net": row["pnl_net"], "fees_est": row["fees_est"],
                "tight_sl": float(meta["_early_tight_sl_px"]),
                "initial_sl": float(meta["stop_loss"]),
            })
        except (KeyError, TypeError, ValueError) as exc:
            print(f"[early_tight_acceptance_replay] 坏记录跳过 trade={row.get('trade_id')}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
    return cohort


def forward_summary(events: list[dict[str, Any]], outcomes: dict[str, dict[str, Any]],
                    rows: list[dict[str, Any]]) -> dict[str, Any]:
    forward_ids = {event.get("trade_id") for event in events if event.get("trade_id") is not None}
    selected = [row for row in rows if row["trade_id"] in forward_ids]
    summary = summarize(selected)
    labeled = [outcomes[event["event_id"]] for event in events
               if event.get("event_id") in outcomes]
    if summary["n"] < MIN_EVAL_N or summary["day_clustered"]["n_days"] < MIN_EVAL_DAYS:
        summary["verdict"] = "TOO_EARLY"
    summary["capture"] = {
        "events": len(events),
        "labeled": len(labeled),
        "close_accepted_2m_n": sum(row.get("close_recovered_2m") is True for row in labeled),
        "hard_sl_during_2m_n": sum(row.get("hard_sl_during_2m") is True for row in labeled),
        "valid_book_n": sum(
            float(event.get("bid") or 0) > 0 and float(event.get("ask") or 0) > 0
            for event in events),
        "bar_mfe_coverage_n": sum(event.get("bar_mfe_pct") is not None for event in events),
        "book_sources": dict(Counter(str(event.get("book_src") or "missing") for event in events)),
    }
    return summary


def run(db_path: Path = DB, bar_dir: Path = BAR_DIR) -> dict[str, Any]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cohort = load_cohort(conn)
    finally:
        conn.close()
    evaluations = []
    exclusions = []
    for trade in cohort:
        path = bar_dir / f"{trade['symbol'].replace('.', '_')}_{trade['entry_time'][:10]}.json"
        if not path.exists():
            exclusions.append({"trade_id": trade["trade_id"], "symbol": trade["symbol"],
                               "status": "no_bars"})
            continue
        bars = json.loads(path.read_text(encoding="utf-8"))
        for optimistic in (False, True):
            result = evaluate_trade(trade, bars, optimistic=optimistic)
            if result["status"] == "ok":
                evaluations.append(result)
            else:
                exclusions.append(result)
    pess = [row for row in evaluations if not row["optimistic"]]
    opt = [row for row in evaluations if row["optimistic"]]
    exclusion_counts = Counter(row["status"] for row in exclusions)
    events = []
    outcomes = {}
    if PENETRATION.exists():
        for line in PENETRATION.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                print(f"[early_tight_acceptance_replay] 坏 jsonl 行跳过: {type(exc).__name__}: {exc}", file=sys.stderr)
                continue
            if row.get("type") == "penetration" and row.get("path") == "early_tight_initial":
                events.append(row)
            elif row.get("type") == "outcome" and row.get("status") == "labeled":
                outcomes[row.get("event_id")] = row
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "design": "DEB-20261105-sl-initial-early-tight-acceptance",
        "fixed_rule": {"full_bars": GRACE_BARS, "max_effective_grace_sec": MAX_EFFECTIVE_GRACE_SEC,
                       "recovery": "bar close inside early-tight", "hard_backstop": "original stop_loss"},
        "cohort_n": len(cohort),
        "exclusions": dict(exclusion_counts),
        "pessimistic": summarize(pess),
        "optimistic": summarize(opt),
        "forward_shadow": forward_summary(events, outcomes, pess),
        "per_trade": {"pessimistic": pess, "optimistic": opt},
        "disclosures": [
            "Actual exit time proxies the first tight-stop touch; broker/close latency is not observable here.",
            "Two full post-trigger 1m bars approximate 120s; effective seconds are reported per trade.",
            "Recovered paths model only original hard SL or EOD after recovery, not the full production exit chain.",
            "Historical n<30 or <10 ET days is exploratory and cannot authorize SIM behavior changes.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()
    report = run()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "per_trade"},
                     ensure_ascii=False, indent=2))
    print(f"→ {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
