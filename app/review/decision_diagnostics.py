"""Read-only decision diagnostics for the daily review bundle."""
from __future__ import annotations

import json
import math
import sqlite3
from collections import OrderedDict
from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Mapping, Tuple

from app.review.bundle_schema import Evidence, Metric
from app.review.stage_stats import load_trades


PRICE_BANDS = (
    ("<$5", 0.0, 5.0),
    ("$5-$10", 5.0, 10.0),
    ("$10-$20", 10.0, 20.0),
    ("$20-$50", 20.0, 50.0),
    ("$50-$100", 50.0, 100.0),
    (">=$100", 100.0, None),
)


def _derived_evidence(
    ref: str,
    report_date: str,
    sample_n: int,
    universe: str = "db:trade_records",
    truncation_checked: bool = True,
) -> Evidence:
    return Evidence(
        kind="derived",
        ref=ref,
        params=(report_date,),
        integrity={
            "n": sample_n,
            "window": f"{report_date}..{report_date}",
            "universe": universe,
            "truncation_checked": truncation_checked,
        },
    )


def _metric(
    name: str,
    value: Any,
    sample_n: int,
    unit: str,
    evidence: Evidence,
) -> Metric:
    return Metric(name=name, value=value, n=sample_n, unit=unit, evidence=evidence)


def _lookback_start(report_date: str, lookback_days: int) -> str:
    return (date.fromisoformat(report_date) - timedelta(days=lookback_days)).isoformat()


def _instruction_map(
    connection: sqlite3.Connection, start: str, end: str
) -> Dict[str, Dict[str, Any]]:
    rows = connection.execute(
        "select decision_id, entry_price, raw_instruction_json, metadata_json "
        "from playbook_instructions where date between ? and ? "
        "and coalesce(decision_id, '') <> '' order by id",
        (start, end),
    ).fetchall()
    result: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        try:
            raw = json.loads(row["raw_instruction_json"] or "{}")
        except (TypeError, ValueError):
            raw = {}
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except (TypeError, ValueError):
            metadata = {}
        result[row["decision_id"]] = {
            "planned_entry": raw.get("planned_entry", row["entry_price"]),
            "chase_cap": raw.get("chase_cap_price"),
            "strategy_type": raw.get("strategy_type"),
            "entry_archetype": raw.get("entry_archetype"),
            "entry_strategy": raw.get("entry_strategy"),
            "reprice": metadata.get("reprice_audit", {}),
        }
    return result


def _enrich_rows(
    connection: sqlite3.Connection, start: str, end: str
) -> List[Dict[str, Any]]:
    rows = [dict(row) for row in load_trades(connection, start, end)]
    instructions = _instruction_map(connection, start, end)
    for row in rows:
        instruction = instructions.get(row.get("decision_id") or "", {})
        row["instruction"] = instruction
        row["prompt_class"] = (
            instruction.get("strategy_type")
            or instruction.get("entry_archetype")
            or instruction.get("entry_strategy")
            or row.get("entry_reason")
            or row.get("strategy_id")
            or "unknown"
        )
    return rows


def build_stage_direction_reason(
    connection: sqlite3.Connection, report_date: str
) -> Dict[str, Dict[str, Dict[str, Dict[str, Metric]]]]:
    rows = _enrich_rows(connection, report_date, report_date)
    grouped: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
    for row in rows:
        key = (row.get("stage") or "", row.get("dir_cn") or "", row.get("exit_reason") or "?")
        grouped.setdefault(key, []).append(row)

    result: Dict[str, Dict[str, Dict[str, Dict[str, Metric]]]] = {}
    for (stage, direction, reason), group in sorted(grouped.items()):
        sample_n = len(group)
        evidence = _derived_evidence(
            "stage x direction x exit_reason aggregation", report_date, len(rows)
        )
        wins = sum(1 for row in group if row["net"] > 0)
        metrics = {
            "n": _metric("n", sample_n, sample_n, "count", evidence),
            "net": _metric(
                "net", round(math.fsum(row["net"] for row in group), 2), sample_n, "usd", evidence
            ),
            "wins": _metric("wins", wins, sample_n, "count", evidence),
            "win_rate": _metric(
                "win_rate", round(wins / sample_n, 6) if sample_n else 0.0,
                sample_n, "ratio", evidence,
            ),
            "mfe0": _metric(
                "mfe0",
                sum(1 for row in group if row.get("mfe_pct") is not None and row["mfe_pct"] <= 0),
                sample_n, "count", evidence,
            ),
        }
        result.setdefault(stage, {}).setdefault(direction, {})[reason] = metrics
    return result


def build_price_band_fees(
    connection: sqlite3.Connection, report_date: str
) -> Dict[str, Dict[str, Metric]]:
    rows = _enrich_rows(connection, report_date, report_date)
    result: Dict[str, Dict[str, Metric]] = {}
    for band, lower, upper in PRICE_BANDS:
        group = [
            row for row in rows
            if row.get("entry_price") is not None
            and float(row["entry_price"]) >= lower
            and (upper is None or float(row["entry_price"]) < upper)
        ]
        if not group:
            continue
        sample_n = len(group)
        evidence = _derived_evidence(
            "entry price band x fee aggregation", report_date, len(rows)
        )
        net = round(math.fsum(row["net"] for row in group), 2)
        gross = round(math.fsum(row["gross"] for row in group), 2)
        fees = round(math.fsum(float(row.get("fees_est") or 0.0) for row in group), 2)
        result[band] = {
            "n": _metric("n", sample_n, sample_n, "count", evidence),
            "net": _metric("net", net, sample_n, "usd", evidence),
            "gross": _metric("gross", gross, sample_n, "usd", evidence),
            "fees": _metric("fees", fees, sample_n, "usd", evidence),
            "fee_to_gross": _metric(
                "fee_to_gross", round(fees / abs(gross), 6) if gross else 0.0,
                sample_n, "ratio", evidence,
            ),
        }
    return result


def _chase_case(row: Mapping[str, Any]) -> Dict[str, Any] | None:
    instruction = row.get("instruction") or {}
    initial_planned = instruction.get("planned_entry")
    reprice = instruction.get("reprice") or {}
    planned = reprice.get("reprice_from", initial_planned)
    actual = row.get("entry_price")
    if planned is None or actual is None or float(planned) <= 0:
        return None
    direction = str(row.get("direction") or "").upper()
    premium = (float(actual) - float(planned)) / float(planned)
    if direction == "SHORT":
        premium = -premium
    chase_cap = instruction.get("chase_cap")
    above_cap = False
    if chase_cap is not None:
        above_cap = (
            float(actual) > float(chase_cap)
            if direction != "SHORT"
            else float(actual) < float(chase_cap)
        )
    if not above_cap and premium <= 0.02:
        return None
    return {
        "trade_id": row.get("id"),
        "symbol": row.get("symbol"),
        "direction": row.get("direction"),
        "stage": row.get("stage"),
        "prompt_class": row.get("prompt_class"),
        "net": round(float(row.get("net") or 0.0), 2),
        "mfe_pct": row.get("mfe_pct"),
        "initial_planned_entry": float(initial_planned) if initial_planned is not None else None,
        "effective_planned_entry": float(planned),
        "actual_entry": float(actual),
        "entry_premium_pct": round(premium, 6),
        "chase_cap": float(chase_cap) if chase_cap is not None else None,
        "above_chase_cap": above_cap,
        "reprice_from": reprice.get("reprice_from"),
        "reprice_to": reprice.get("reprice_to"),
        "reprice_premium_pct": round(
            (float(actual) - float(reprice["reprice_from"])) / float(reprice["reprice_from"]), 6
        ) if reprice.get("reprice_from") not in (None, "", 0) else None,
        "verdict": "candidate_not_verdict",
    }


def _mfe0_case(row: Mapping[str, Any]) -> Dict[str, Any] | None:
    if row.get("mfe_pct") is None or float(row["mfe_pct"]) > 0:
        return None
    if float(row.get("net") or 0.0) >= 0:
        return None
    return {
        "trade_id": row.get("id"),
        "symbol": row.get("symbol"),
        "direction": row.get("direction"),
        "stage": row.get("stage"),
        "prompt_class": row.get("prompt_class"),
        "net": round(float(row.get("net") or 0.0), 2),
        "mfe_pct": row.get("mfe_pct"),
        "mae_pct": row.get("mae_pct"),
        "entry_price": row.get("entry_price"),
        "decision_id": row.get("decision_id"),
        "verdict": "candidate_not_verdict",
        "hint": "MFE=0 and net loss: direction or entry price is suspect; verify prompt and reprice",
    }


def build_repeated_loss_clusters(
    connection: sqlite3.Connection,
    report_date: str,
    lookback_days: int = 30,
) -> List[Dict[str, Any]]:
    start = _lookback_start(report_date, lookback_days)
    rows = _enrich_rows(connection, start, report_date)
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("prompt_class") or "unknown"), []).append(row)

    clusters = []
    for prompt_class, group in sorted(grouped.items()):
        group.sort(key=lambda row: (str(row.get("exit_time") or ""), row.get("id") or 0))
        streak = []
        for row in reversed(group):
            if float(row.get("net") or 0.0) > 0:
                break
            streak.append(row)
        streak.reverse()
        streak_loss = round(math.fsum(float(row.get("net") or 0.0) for row in streak), 2)
        days = {str(row.get("exit_time") or "")[:10] for row in group}
        symbols = {row.get("symbol") for row in group}
        sample_n = len(group)
        qualifies = (
            sample_n >= 3
            and len(days) >= 2
            and len(symbols) >= 2
            and len(streak) >= 3
            and streak_loss < -1000
        )
        evidence = _derived_evidence(
            "repeated loss cluster aggregation", report_date, len(rows),
            "db:trade_records+db:playbook_instructions",
        )
        clusters.append(
            {
                "prompt_class": prompt_class,
                "window": f"{start}..{report_date}",
                "n": _metric("n", sample_n, sample_n, "count", evidence),
                "distinct_days": _metric("distinct_days", len(days), sample_n, "count", evidence),
                "distinct_symbols": _metric(
                    "distinct_symbols", len(symbols), sample_n, "count", evidence
                ),
                "current_loss_streak": _metric(
                    "current_loss_streak", len(streak), sample_n, "count", evidence
                ),
                "current_streak_loss": _metric(
                    "current_streak_loss", streak_loss, sample_n, "usd", evidence
                ),
                "cluster_total_net": _metric(
                    "cluster_total_net",
                    round(math.fsum(float(row.get("net") or 0.0) for row in group), 2),
                    sample_n, "usd", evidence,
                ),
                "weak_interruptions": _metric(
                    "weak_interruptions",
                    sum(1 for row in group if 0 < float(row.get("net") or 0.0) <= 50),
                    sample_n, "count", evidence,
                ),
                "qualifies": _metric("qualifies", qualifies, sample_n, "bool", evidence),
                "symbols": _metric("symbols", sorted(str(x) for x in symbols), sample_n, "text", evidence),
            }
        )
    return clusters


def build_sim_real_bridge(
    connection: sqlite3.Connection,
    report_date: str,
    lookback_days: int = 30,
) -> Dict[str, Metric]:
    start = _lookback_start(report_date, lookback_days)
    rows = _enrich_rows(connection, start, report_date)
    sim_rows: Dict[str, Dict[str, Any]] = {}
    real_rows: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        source = str(row.get("source_type") or "")
        mode = str(row.get("trade_mode") or "")
        pair_key = row.get("signal_id") or row.get("decision_id")
        if not pair_key:
            continue
        if source == "PLAYBOOK_SIM" or mode in ("simulate", "sim"):
            sim_rows.setdefault(str(pair_key), row)
        elif source == "PLAYBOOK_LIVE" or mode in ("live", "real"):
            real_rows.setdefault(str(pair_key), row)

    pairs = []
    for key in sorted(set(sim_rows) & set(real_rows)):
        sim = sim_rows[key]
        real = real_rows[key]
        difference = float(real.get("net") or 0.0) - float(sim.get("net") or 0.0)
        sim_notional = float(sim.get("entry_price") or 0.0) * float(sim.get("quantity") or 0.0)
        normalized = difference / sim_notional * 5000 if sim_notional else 0.0
        pairs.append(
            {
                "pair_key": key,
                "symbol": real.get("symbol"),
                "prompt_class": real.get("prompt_class"),
                "stage": real.get("stage"),
                "strategy_family": real.get("strategy_id"),
                "sim_net": round(float(sim.get("net") or 0.0), 2),
                "real_net": round(float(real.get("net") or 0.0), 2),
                "real_minus_sim": round(difference, 2),
                "normalized_per_5000": round(normalized, 2),
            }
        )
    rolling_difference = round(math.fsum(pair["real_minus_sim"] for pair in pairs[-20:]), 2)
    sample_n = len(rows)
    evidence = _derived_evidence(
        "same-signal SIM/live paired comparison", report_date, sample_n,
        "db:trade_records+db:playbook_instructions",
    )
    return {
        "paired_signal_count": _metric(
            "paired_signal_count", len(pairs), sample_n, "count", evidence
        ),
        "sim_trade_count": _metric(
            "sim_trade_count", len(sim_rows), sample_n, "count", evidence
        ),
        "real_trade_count": _metric(
            "real_trade_count", len(real_rows), sample_n, "count", evidence
        ),
        "rolling_20_cumulative_difference": _metric(
            "rolling_20_cumulative_difference", rolling_difference, sample_n, "usd", evidence
        ),
        "normalized_per_5000": _metric(
            "normalized_per_5000",
            round(math.fsum(pair["normalized_per_5000"] for pair in pairs[-20:]), 2),
            sample_n, "usd", evidence,
        ),
        "rolling_20_exceeds_500": _metric(
            "rolling_20_exceeds_500", rolling_difference > 500, sample_n, "bool", evidence
        ),
        "pairs": _metric("pairs", pairs, sample_n, "text", evidence),
    }


def build_decision_diagnostics(
    connection: sqlite3.Connection,
    report_date: str,
    lookback_days: int = 30,
) -> Dict[str, Any]:
    rows = _enrich_rows(connection, report_date, report_date)
    mfe0_cases = [case for case in map(_mfe0_case, rows) if case is not None]
    chase_cases = [case for case in map(_chase_case, rows) if case is not None]
    repeated = build_repeated_loss_clusters(connection, report_date, lookback_days)
    bridge = build_sim_real_bridge(connection, report_date, lookback_days)
    evidence = _derived_evidence(
        "daily three-question diagnostic aggregation", report_date, len(rows),
        "db:trade_records+db:playbook_instructions",
    )
    questions = {
        "should_not_fire_count": _metric(
            "should_not_fire_count", len(mfe0_cases), len(rows), "count", evidence
        ),
        "should_not_fire_cases": _metric(
            "should_not_fire_cases", mfe0_cases, len(rows), "text", evidence
        ),
        "chase_entry_count": _metric(
            "chase_entry_count", len(chase_cases), len(rows), "count", evidence
        ),
        "chase_entry_cases": _metric(
            "chase_entry_cases", chase_cases, len(rows), "text", evidence
        ),
        "repeated_pitfall_count": _metric(
            "repeated_pitfall_count", sum(1 for cluster in repeated if cluster["qualifies"].value),
            len(rows), "count", evidence,
        ),
        "repeated_pitfall_clusters": _metric(
            "repeated_pitfall_clusters", repeated, len(rows), "text", evidence
        ),
    }
    return {
        "three_questions": questions,
        "stage_direction_reason": build_stage_direction_reason(connection, report_date),
        "price_band_fees": build_price_band_fees(connection, report_date),
        "sim_real_bridge": bridge,
    }
