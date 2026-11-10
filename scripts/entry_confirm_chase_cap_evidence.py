#!/usr/bin/env python3
"""Validate existing entry-confirm chase-cap proposals with exact live arm anchors.

Frozen candidates:
  * RFC-170: all confirmed archetypes, opp_cost_pct >= 0.5%.
  * RFC-153: FADE_SHORT in the opening 15 minutes, opp_cost_pct >= 1.0%.

The primary evidence is the archived forward JSONL joined to realized SIM PnL.
It uses the same arm anchor as production.  Rows after the RFC-170 shadow field
appeared are reported separately as the small forward-validation cohort.  No
minute-bar entry-price proxy is mixed into this arm-anchor audit.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from entry_confirm_variant_replay import _boot_ci_day_clustered, _day_clustered_t  # noqa: E402

DB = REPO / "data" / "ftseek_us.db"
FORWARD_ARCHIVE = REPO / "data" / "shadow" / "entry_confirm_forward_archive_20261012_20261104.jsonl"
ACTIVE_FORWARD = REPO / "data" / "shadow" / "entry_confirm_forward.jsonl"
OUT_JSON = REPO / "data" / "reports" / "entry_confirm_chase_cap_evidence_20261105.json"

MIN_CAPPED_N = 30
MIN_CAPPED_DAYS = 10
BOOTSTRAP = 10_000
SEED = 20261105


def _number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _jsonl(path):
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                print(f"[entry_confirm_chase_cap_evidence] 坏 jsonl 行跳过 {path.name}: {type(exc).__name__}: {exc}", file=sys.stderr)
                continue
    return rows


def _is_fade_open(row):
    return bool(
        row.get("entry_archetype") == "FADE_SHORT"
        and isinstance(row.get("session_min"), int)
        and 0 <= row["session_min"] < 15
    )


def cap_hit(row, threshold_pct, fade_open_only=False):
    if row.get("type") != "entry_confirm" or row.get("outcome") != "CONFIRMED":
        return False
    if fade_open_only and not _is_fade_open(row):
        return False
    opp = _number(row.get("opp_cost_pct"))
    return opp is not None and opp >= threshold_pct


def archived_policy_delta(row, trade, threshold_pct, fade_open_only=False):
    """Candidate minus existing confirmed policy, using actual delayed-fill PnL."""
    if not cap_hit(row, threshold_pct, fade_open_only):
        return 0.0
    pnl = _number((trade or {}).get("pnl_net"))
    return -pnl if pnl is not None else 0.0


def _robustness(rows, deltas, n_capped, capped_days, bootstrap=BOOTSTRAP):
    by_day = defaultdict(list)
    for row, delta in zip(rows, deltas):
        by_day[row["date"]].append(delta)
    pooled = [value for values in by_day.values() for value in values]
    dates = sorted(by_day)
    split = dates[len(dates) // 2] if dates else None
    first = [v for day, values in by_day.items() if split and day < split for v in values]
    second = [v for day, values in by_day.items() if split and day >= split for v in values]
    worst_three = {
        day for day, _ in sorted(
            ((day, sum(values)) for day, values in by_day.items()), key=lambda item: item[1]
        )[:3]
    }
    trimmed = [v for day, values in by_day.items() if day not in worst_three for v in values]
    ci = _boot_ci_day_clustered(by_day, seed=SEED, n_boot=bootstrap) if len(dates) >= 3 else None
    sign_flip_p = _sign_flip_p_day_clustered(by_day, bootstrap) if len(dates) >= 3 else None
    first_mean = statistics.mean(first) if first else None
    second_mean = statistics.mean(second) if second else None
    trim_mean = statistics.mean(trimmed) if trimmed else None
    day_means = [sum(values) / len(values) for values in by_day.values()]
    return {
        "joined_n": len(rows),
        "joined_days": len(dates),
        "n_capped": n_capped,
        "capped_days": capped_days,
        "delta_sum_usd": round(sum(pooled), 2),
        "delta_mean_usd": round(statistics.mean(pooled), 4) if pooled else None,
        "day_clustered": _day_clustered_t(day_means),
        "bootstrap_ci95": ci,
        "sign_flip_day_cluster_p_two_sided": sign_flip_p,
        "split_date": split,
        "first_half_mean_usd": round(first_mean, 4) if first_mean is not None else None,
        "second_half_mean_usd": round(second_mean, 4) if second_mean is not None else None,
        "trim_worst_3d_mean_usd": round(trim_mean, 4) if trim_mean is not None else None,
        "gates": {
            "sample": n_capped >= MIN_CAPPED_N and capped_days >= MIN_CAPPED_DAYS,
            "ci_positive": bool(ci and ci["ci95_lo"] > 0),
            "sign_flip_p_lt_0_05": bool(sign_flip_p is not None and sign_flip_p < 0.05),
            "half_windows_positive": bool(first_mean is not None and second_mean is not None
                                          and first_mean > 0 and second_mean > 0),
            "trimmed_positive": bool(trim_mean is not None and trim_mean > 0),
        },
    }


def _sign_flip_p_day_clustered(by_day, n_boot=BOOTSTRAP):
    """Two-sided paired sign-flip test, flipping whole days to retain clustering."""
    days = sorted(by_day)
    observed_values = [value for day in days for value in by_day[day]]
    if not observed_values or all(value == 0 for value in observed_values):
        return 1.0
    observed = abs(statistics.mean(observed_values))
    rng = random.Random(SEED + 1)
    extreme = 0
    for _ in range(n_boot):
        signs = {day: (-1 if rng.getrandbits(1) else 1) for day in days}
        sampled = [
            signs[day] * value
            for day in days for value in by_day[day]
        ]
        extreme += abs(statistics.mean(sampled)) >= observed - 1e-12
    return round((extreme + 1) / (n_boot + 1), 6)


def evaluate_confirmed(archive_rows, trades, threshold_pct, fade_open_only=False,
                       require_shadow_field=False, bootstrap=BOOTSTRAP):
    selected = []
    unjoined = 0
    for row in archive_rows:
        if row.get("type") != "entry_confirm" or row.get("outcome") != "CONFIRMED":
            continue
        if require_shadow_field and row.get("chase_shadow_would_abandon") is None:
            continue
        if fade_open_only and not _is_fade_open(row):
            continue
        trade = trades.get(row.get("decision_id"))
        if not trade or _number(trade.get("pnl_net")) is None:
            unjoined += 1
            continue
        selected.append((row, trade))
    rows = [row for row, _ in selected]
    deltas = [archived_policy_delta(row, trade, threshold_pct, fade_open_only)
              for row, trade in selected]
    capped = [(row, trade) for row, trade in selected
              if cap_hit(row, threshold_pct, fade_open_only)]
    result = _robustness(rows, deltas, len(capped), len({row["date"] for row, _ in capped}), bootstrap)
    result.update({
        "threshold_pct": threshold_pct,
        "confirmed_rows_with_unjoined": len(rows) + unjoined,
        "unjoined_n": unjoined,
        "capped_winner_n": sum(float(trade["pnl_net"]) > 0 for _, trade in capped),
        "capped_loser_n": sum(float(trade["pnl_net"]) < 0 for _, trade in capped),
        "capped_actual_pnl_sum_usd": round(sum(float(trade["pnl_net"]) for _, trade in capped), 2),
        "capped_actual_gross_pnl_sum_usd": round(
            sum(float(trade["realized_pnl"]) for _, trade in capped
                if _number(trade.get("realized_pnl")) is not None), 2),
        "capped_saved_fees_sum_usd": round(sum(
            float(trade["realized_pnl"]) - float(trade["pnl_net"]) for _, trade in capped
            if _number(trade.get("realized_pnl")) is not None
        ), 2),
        "capped": [{
            "date": row["date"], "symbol": row["symbol"],
            "entry_archetype": row.get("entry_archetype"), "session_min": row.get("session_min"),
            "opp_cost_pct": row["opp_cost_pct"], "pnl_net": trade["pnl_net"],
            "exit_reason": trade.get("exit_reason"),
        } for row, trade in capped],
    })
    result["pass"] = all(result["gates"].values())
    return result


def load_trades_and_archetypes(db_path=DB):
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    trades = {
        row["decision_id"]: dict(row) for row in con.execute(
            "SELECT decision_id, realized_pnl, pnl_net, exit_reason, trade_date FROM trade_records "
            "WHERE source_type='PLAYBOOK_SIM' AND decision_id != ''"
        )
    }
    archetypes = {}
    for row in con.execute(
        "SELECT decision_id, raw_instruction_json FROM playbook_instructions "
        "WHERE decision_id IS NOT NULL AND decision_id != ''"
    ):
        try:
            archetype = json.loads(row["raw_instruction_json"] or "{}").get("entry_archetype")
        except (TypeError, json.JSONDecodeError):
            archetype = None
        if archetype:
            archetypes[row["decision_id"]] = archetype
    con.close()
    return trades, archetypes


def _directional_move_pct(direction, entry_price, future_price):
    entry = _number(entry_price)
    future = _number(future_price)
    if not entry or future is None:
        return None
    sign = 1 if direction == "LONG" else -1
    return round(sign * (future - entry) / entry * 100, 4)


def actual_fade_cap_abandons(archive_rows):
    post_by_inst = defaultdict(list)
    for row in archive_rows:
        if row.get("type") == "entry_confirm_post":
            post_by_inst[row.get("inst_id")].append(row)
    results = []
    for row in archive_rows:
        if not row.get("chase_cap_hit"):
            continue
        post = [{
            "horizon": watch.get("horizon"),
            "move_from_abandon_px_pct": _directional_move_pct(
                row.get("direction"), row.get("px_at_confirm"), watch.get("px_last")
            ),
        } for watch in post_by_inst.get(row.get("inst_id"), [])]
        results.append({
            "date": row.get("date"), "symbol": row.get("symbol"),
            "opp_cost_pct": row.get("opp_cost_pct"), "post_watch": post,
        })
    return results


def build_report(bootstrap=BOOTSTRAP, db_path=DB, forward_archive_path=FORWARD_ARCHIVE,
                 active_forward_path=ACTIVE_FORWARD):
    archive = _jsonl(forward_archive_path)
    trades, archetypes = load_trades_and_archetypes(db_path)
    for row in archive:
        if not row.get("entry_archetype") and row.get("decision_id") in archetypes:
            row["entry_archetype"] = archetypes[row["decision_id"]]
    candidates = {}
    for name, threshold, fade_only in (
        ("global_0_5pct", 0.5, False),
        ("fade_short_open_1_0pct", 1.0, True),
    ):
        historical = evaluate_confirmed(archive, trades, threshold, fade_only, False, bootstrap)
        forward = evaluate_confirmed(archive, trades, threshold, fade_only, True, bootstrap)
        deployable = historical["pass"] and forward["pass"]
        candidates[name] = {
            "archived_all": historical,
            "archived_forward": forward,
            "deployable": deployable,
            "verdict": "DEPLOYABLE" if deployable else "NO_DEPLOY",
        }
    return {
        "generated_at": datetime.now().astimezone().isoformat(),
        "mode": "read-only exact-arm-anchor audit; no SIM behavior change",
        "pre_registered_candidates_only": True,
        "sample_gate": {"min_capped_n": MIN_CAPPED_N, "min_capped_days": MIN_CAPPED_DAYS},
        "archive_coverage": {
            "decision_rows": sum(row.get("type") == "entry_confirm" for row in archive),
            "confirmed_rows": sum(row.get("outcome") == "CONFIRMED" for row in archive),
            "joined_sim_trades": len(trades),
            "archetypes_backfilled_from_instructions": True,
        },
        "current_cohort": {
            "active_jsonl_rows": len(_jsonl(active_forward_path)),
            "note": "RFC-158 cohort started 2026-11-05; archived rows cannot fill its sample gate.",
        },
        "candidates": candidates,
        "actual_fade_cap_abandons": actual_fade_cap_abandons(archive),
        "disclosures": [
            "archived_all includes the data that motivated the thresholds; it is not untouched OOS.",
            "Only archived_forward rows with the RFC-170 shadow field are separated as later forward evidence.",
            "Cap delta against an existing confirmed trade is -pnl_net; opp_cost is not added again.",
            "Primary inference uses net PnL because abandon saves fees; gross PnL and saved fees are reported alongside.",
            "Abandoned +5m/+10m watches are short-horizon price paths, not full counterfactual trade PnL.",
            "The current RFC-158 cohort is evaluated separately and cannot borrow archived sample counts.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUT_JSON)
    parser.add_argument("--bootstrap", type=int, default=BOOTSTRAP)
    args = parser.parse_args()
    report = build_report(bootstrap=args.bootstrap)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"current RFC-158 rows: {report['current_cohort']['active_jsonl_rows']}")
    for name, candidate in report["candidates"].items():
        historical = candidate["archived_all"]
        forward = candidate["archived_forward"]
        print(
            f"{name}: {candidate['verdict']} | archived capped={historical['n_capped']} "
            f"delta=${historical['delta_sum_usd']} gates={historical['gates']} | "
            f"forward capped={forward['n_capped']} delta=${forward['delta_sum_usd']} "
            f"gates={forward['gates']}"
        )
    print(f"actual FADE cap abandons: {len(report['actual_fade_cap_abandons'])}")
    print(f"report: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
