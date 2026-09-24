"""Evidence-bearing result layer for the daily review bundle."""
from __future__ import annotations

import logging
import math
import sqlite3
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping

from app.review.actionability_diagnostics import build_actionability_diagnostics
from app.review.bundle_schema import Bundle, Evidence, Metric
from app.review.decision_diagnostics import build_decision_diagnostics
from app.review.stage_stats import load_trades, stage_breakdown
from app.utils.timezone import to_et


_DIRECTION_TO_PLAN = {"LONG": "BUY", "SHORT": "SELL_SHORT"}

_log = logging.getLogger(__name__)


def _derived_evidence(
    ref: str,
    report_date: str,
    sample_n: int,
    universe: str = "db:trade_records",
    truncation_checked: bool = True,
    note: str = "",
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
        note=note,
    )


def _metric(name: str, value: Any, sample_n: int, unit: str, evidence: Evidence) -> Metric:
    return Metric(name=name, value=value, n=sample_n, unit=unit, evidence=evidence)


def _result_metrics(rows: Iterable[Mapping[str, Any]], report_date: str) -> Dict[str, Metric]:
    ordered_rows = list(rows)
    sample_n = len(ordered_rows)
    evidence = _derived_evidence(
        "app.review.stage_stats.load_trades then deterministic aggregation",
        report_date,
        sample_n,
        truncation_checked=all(row.get("exit_time") is not None for row in ordered_rows),
        note="full-day query has no LIMIT and every selected closed trade carries exit_time",
    )
    net = round(math.fsum(float(row.get("net") or 0.0) for row in ordered_rows), 2)
    gross = round(math.fsum(float(row.get("gross") or 0.0) for row in ordered_rows), 2)
    fees = round(math.fsum(float(row.get("fees_est") or 0.0) for row in ordered_rows), 2)
    wins = sum(1 for row in ordered_rows if float(row.get("net") or 0.0) > 0)
    mfe0 = sum(
        1
        for row in ordered_rows
        if row.get("mfe_pct") is not None and float(row.get("mfe_pct") or 0.0) <= 0
    )
    win_rate = round(wins / sample_n, 6) if sample_n else 0.0
    fee_to_gross = round(fees / abs(gross), 6) if gross else 0.0

    return {
        "net_pnl": _metric("net_pnl", net, sample_n, "usd", evidence),
        "gross_pnl": _metric("gross_pnl", gross, sample_n, "usd", evidence),
        "fees": _metric("fees", fees, sample_n, "usd", evidence),
        "fee_to_gross": _metric("fee_to_gross", fee_to_gross, sample_n, "ratio", evidence),
        "trade_count": _metric("trade_count", sample_n, sample_n, "count", evidence),
        "win_count": _metric("win_count", wins, sample_n, "count", evidence),
        "win_rate": _metric("win_rate", win_rate, sample_n, "ratio", evidence),
        "mfe0_count": _metric("mfe0_count", mfe0, sample_n, "count", evidence),
    }


def build_slices(
    connection: sqlite3.Connection, report_date: str
) -> Dict[str, Dict[str, Metric]]:
    """Build ordered stage slices; each numeric field carries evidence."""
    rows = load_trades(connection, report_date, report_date)
    breakdown = stage_breakdown(rows)
    sample_n = len(rows)

    slices: Dict[str, Dict[str, Metric]] = {}
    for stage, values in breakdown.items():
        stage_rows = [row for row in rows if (row.get("stage") or "") == stage]
        evidence = _derived_evidence(
            "app.review.stage_stats.stage_breakdown over load_trades rows",
            report_date,
            sample_n,
            truncation_checked=all(row.get("exit_time") is not None for row in rows),
            note="full-day query has no LIMIT and every selected closed trade carries exit_time",
        )
        stage_n = int(values["n"])
        wins = int(values["wins"])
        win_rate = round(wins / stage_n, 6) if stage_n else 0.0
        slices[stage] = {
            "n": _metric("n", stage_n, stage_n, "count", evidence),
            "net": _metric("net", round(float(values["net"]), 2), stage_n, "usd", evidence),
            "gross": _metric(
                "gross",
                round(math.fsum(float(row.get("gross") or 0.0) for row in stage_rows), 2),
                stage_n,
                "usd",
                evidence,
            ),
            "wins": _metric("wins", wins, stage_n, "count", evidence),
            "win_rate": _metric("win_rate", win_rate, stage_n, "ratio", evidence),
            "mfe0": _metric("mfe0", int(values["mfe0"]), stage_n, "count", evidence),
        }
    return slices


def build_plan_vs_actual(
    connection: sqlite3.Connection, report_date: str
) -> Dict[str, Metric]:
    """Compare playbook instruction pairs with actual playbook trades."""
    instructions = connection.execute(
        "select symbol, direction from playbook_instructions "
        "where date = ? and direction in ('BUY', 'SELL_SHORT') order by id",
        (report_date,),
    ).fetchall()
    trades = connection.execute(
        "select symbol, direction from trade_records "
        "where trade_date = ? and source_type in ('PLAYBOOK_SIM', 'PLAYBOOK_LIVE') "
        "and (exit_reason is null or exit_reason != 'PHANTOM_RECONCILE_VOID') "
        "order by id",
        (report_date,),
    ).fetchall()

    planned_pairs = list(
        dict.fromkeys((row["symbol"], row["direction"]) for row in instructions)
    )
    actual_pairs = list(
        dict.fromkeys(
            (
                row["symbol"],
                _DIRECTION_TO_PLAN.get(row["direction"], row["direction"]),
            )
            for row in trades
        )
    )
    planned_lookup = {pair: index for index, pair in enumerate(planned_pairs)}
    actual_lookup = {pair: index for index, pair in enumerate(actual_pairs)}
    executed_pairs = sorted(
        (pair for pair in planned_lookup if pair in actual_lookup),
        key=lambda pair: planned_lookup[pair],
    )
    missed_pairs = sorted(
        (pair for pair in planned_lookup if pair not in actual_lookup),
        key=lambda pair: planned_lookup[pair],
    )
    unplanned_pairs = sorted(
        (pair for pair in actual_lookup if pair not in planned_lookup),
        key=lambda pair: actual_lookup[pair],
    )
    direction_mismatch = []
    actual_by_symbol: Dict[str, str] = {}
    for symbol, direction in actual_pairs:
        actual_by_symbol.setdefault(symbol, direction)
    for symbol, planned_direction in planned_pairs:
        actual_direction = actual_by_symbol.get(symbol)
        if actual_direction is not None and actual_direction != planned_direction:
            direction_mismatch.append(f"{symbol}({planned_direction}/{actual_direction})")

    sample_n = len(planned_pairs)
    evidence = _derived_evidence(
        "playbook_instructions and trade_records pair comparison",
        report_date,
        sample_n,
        universe="db:playbook_instructions+db:trade_records",
        note="both full-day queries have no LIMIT; pair coverage is built from both sources",
    )
    executed_count = len(executed_pairs)
    execution_rate = round(executed_count / sample_n, 6) if sample_n else 0.0
    return {
        "plan_available": _metric(
            "plan_available", bool(planned_pairs), sample_n, "bool", evidence
        ),
        "planned_count": _metric("planned_count", sample_n, sample_n, "count", evidence),
        "executed_count": _metric(
            "executed_count", executed_count, sample_n, "count", evidence
        ),
        "missed_count": _metric("missed_count", len(missed_pairs), sample_n, "count", evidence),
        "unplanned_count": _metric(
            "unplanned_count", len(unplanned_pairs), sample_n, "count", evidence
        ),
        "execution_rate": _metric(
            "execution_rate", execution_rate, sample_n, "ratio", evidence
        ),
        "direction_mismatch": _metric(
            "direction_mismatch", direction_mismatch, sample_n, "text", evidence
        ),
        "unplanned_symbols": _metric(
            "unplanned_symbols", [pair[0] for pair in unplanned_pairs], sample_n, "text", evidence
        ),
    }


def exit_date_mismatches(connection: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Return closed rows whose raw date and ET exit date disagree."""
    rows = connection.execute(
        "select id, exit_time from trade_records "
        "where exit_time is not null and pnl_net is not null "
        "and coalesce(data_quality, '') <> 'duplicate_external_close' "
        "order by id"
    ).fetchall()
    mismatches = []
    for row in rows:
        raw_exit_time = row["exit_time"]
        sqlite_date = str(raw_exit_time)[:10]
        try:
            parsed = datetime.fromisoformat(str(raw_exit_time).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            _log.warning("exit time parse failed", exc_info=True)
            continue
        et_date = to_et(parsed).date().isoformat()
        if sqlite_date != et_date:
            mismatches.append(
                {
                    "trade_id": row["id"],
                    "sqlite_date": sqlite_date,
                    "et_date": et_date,
                }
            )
    return mismatches


def build_anomalies(
    connection: sqlite3.Connection, report_date: str
) -> List[Dict[str, Any]]:
    anomalies = []
    for row in exit_date_mismatches(connection):
        if row["et_date"] != report_date:
            continue
        anomalies.append(
            {
                "kind": "exit_date_mismatch",
                "severity": "P0",
                "trade_id": row["trade_id"],
                "sqlite_date": row["sqlite_date"],
                "et_date": row["et_date"],
            }
        )
    return anomalies


def build(connection: sqlite3.Connection, report_date: str) -> Bundle:
    rows = load_trades(connection, report_date, report_date)
    return Bundle(
        report_date=report_date,
        headline=_result_metrics(rows, report_date),
        sections={
            "slices": build_slices(connection, report_date),
            "plan_vs_actual": build_plan_vs_actual(connection, report_date),
            "decision_diagnostics": build_decision_diagnostics(connection, report_date),
            "actionability": build_actionability_diagnostics(connection, report_date),
        },
        anomalies=build_anomalies(connection, report_date),
        self_audit={
            "exit_date_mismatch_count": len(
                [row for row in exit_date_mismatches(connection) if row["et_date"] == report_date]
            )
        },
    )
