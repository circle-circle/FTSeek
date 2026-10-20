"""Evidence-bearing result layer for the daily review bundle."""
from __future__ import annotations

import json
import logging
import math
import sqlite3
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping

from app.review.actionability_diagnostics import build_actionability_diagnostics
from app.review.bundle_schema import Bundle, Evidence, Metric
from app.review.decision_diagnostics import build_decision_diagnostics
from app.review.stage_stats import load_trades, stage_breakdown
from app.utils.timezone import to_et

logger = logging.getLogger(__name__)

_DIRECTION_TO_PLAN = {"LONG": "BUY", "SHORT": "SELL_SHORT"}
_PULLBACK_SHADOW = (
    Path(__file__).resolve().parents[2] / "data" / "shadow" / "pullback_limit_shadow.jsonl"
)
_POST_EXIT_SHADOW = (
    Path(__file__).resolve().parents[2] / "data" / "shadow" / "post_exit_path.jsonl"
)

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
    """Compare playbook instruction pairs with actual playbook trades.

    2026-10-20 P0: 2 分钟门放弃 (`skip_reason='entry_confirm_reversed'`) 必须进
    planned 分母。该行 direction 已被改成 SKIP, 旧查询只看 BUY/SELL_SHORT ⇒ 目标
    从分母消失, 执行率虚高 (10-19 实跑 7/7=100%, 正确口径 7/8=87.5%)。
    """
    instructions = connection.execute(
        "select symbol, direction, skip_reason, entry_price, "
        "       coalesce("
        "         json_extract(raw_instruction_json, '$.confirmation_gap_pct'), "
        "         case when json_extract(raw_instruction_json, '$.confirmation.price_level') is not null "
        "                   and entry_price > 0 "
        "              then (json_extract(raw_instruction_json, '$.confirmation.price_level') "
        "                    - entry_price) / entry_price * 100 end"
        "       ) as gap_pct, "
        "       json_extract(raw_instruction_json, '$.entry_archetype') as archetype "
        "from playbook_instructions "
        "where date = ? and (direction in ('BUY', 'SELL_SHORT') "
        "or skip_reason = 'entry_confirm_reversed') order by id",
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
    abandoned_pairs = list(
        dict.fromkeys(
            (row["symbol"], row["direction"])
            for row in instructions
            if (row["skip_reason"] or "") == "entry_confirm_reversed"
        )
    )
    abandoned_lookup = set(abandoned_pairs)
    plan_gap_pairs = list(
        dict.fromkeys(
            (row["symbol"], row["direction"])
            for row in instructions
            if row["gap_pct"] is not None
            and abs(float(row["gap_pct"])) > 1.0
            and str(row["archetype"] or "").upper() in ("PULLBACK_LIMIT", "FADE_SHORT")
        )
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
        (pair for pair in planned_lookup
         if pair not in actual_lookup and pair not in abandoned_lookup),
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
        "abandoned_count": _metric(
            "abandoned_count", len(abandoned_pairs), sample_n, "count", evidence
        ),
        "abandoned_symbols": _metric(
            "abandoned_symbols", [pair[0] for pair in abandoned_pairs], sample_n, "text", evidence
        ),
        "plan_gap_count": _metric(
            "plan_gap_count", len(plan_gap_pairs), sample_n, "count", evidence
        ),
        "plan_gap_symbols": _metric(
            "plan_gap_symbols", [pair[0] for pair in plan_gap_pairs], sample_n, "text", evidence
        ),
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


def _parse_metadata(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def build_research_arm_split(
    connection: sqlite3.Connection, report_date: str
) -> Dict[str, Metric]:
    """Split the headline cohort into research arm vs the rest (read-only).

    2026-10-20 P0: 研究臂 (gate_mode=research_momentum) 自 10-19 起写
    PLAYBOOK_SIM, 但它是独立研究源, 不经过 DA 入场确认门。日报 headline 仍统计
    全部成交 (总账), 本 section 只让读者看到分账, 不改 headline/OKR 判分 SQL。
    """
    rows = load_trades(connection, report_date, report_date)
    research = []
    core = []
    sources: Dict[str, int] = {}
    for row in rows:
        meta = _parse_metadata(row.get("metadata_json"))
        if str(meta.get("gate_mode") or "") == "research_momentum":
            research.append(row)
            source = str(meta.get("research_source") or "unknown")
            sources[source] = sources.get(source, 0) + 1
        else:
            core.append(row)

    open_rows = connection.execute(
        "select symbol, metadata_json from trade_records "
        "where exit_time is null and source_type in ('PLAYBOOK_SIM', 'PLAYBOOK_LIVE') "
        "order by id"
    ).fetchall()
    open_research = []
    open_sources: Dict[str, int] = {}
    for row in open_rows:
        meta = _parse_metadata(row["metadata_json"])
        if str(meta.get("gate_mode") or "") != "research_momentum":
            continue
        open_research.append(row["symbol"])
        source = str(meta.get("research_source") or "unknown")
        open_sources[source] = open_sources.get(source, 0) + 1

    n = len(rows)
    evidence = _derived_evidence(
        "app.review.stage_stats.load_trades then metadata gate_mode split",
        report_date,
        n,
        universe="db:trade_records",
        note="closed trades only (same cohort as headline); open research positions counted separately",
    )
    open_evidence = _derived_evidence(
        "db:trade_records where exit_time is null",
        report_date,
        len(open_rows),
        universe="db:trade_records",
        note="open positions, research arm classification from metadata gate_mode",
    )

    def _sum(rows_, key):
        return round(math.fsum(float(row.get(key) or 0.0) for row in rows_), 2)

    def _wins(rows_):
        return sum(1 for row in rows_ if float(row.get("net") or 0.0) > 0)

    def _rate(rows_):
        return round(_wins(rows_) / len(rows_), 6) if rows_ else 0.0

    source_text = ", ".join(
        f"{source} {count}" for source, count in sorted(sources.items())
    )
    return {
        "research_trade_count": _metric(
            "research_trade_count", len(research), n, "count", evidence
        ),
        "research_net_pnl": _metric(
            "research_net_pnl", _sum(research, "net"), n, "usd", evidence
        ),
        "research_gross_pnl": _metric(
            "research_gross_pnl", _sum(research, "gross"), n, "usd", evidence
        ),
        "research_fees": _metric(
            "research_fees", _sum(research, "fees_est"), n, "usd", evidence
        ),
        "research_win_count": _metric(
            "research_win_count", _wins(research), n, "count", evidence
        ),
        "research_win_rate": _metric(
            "research_win_rate", _rate(research), n, "ratio", evidence
        ),
        "core_trade_count": _metric(
            "core_trade_count", len(core), n, "count", evidence
        ),
        "core_net_pnl": _metric(
            "core_net_pnl", _sum(core, "net"), n, "usd", evidence
        ),
        "core_win_count": _metric(
            "core_win_count", _wins(core), n, "count", evidence
        ),
        "core_win_rate": _metric(
            "core_win_rate", _rate(core), n, "ratio", evidence
        ),
        "research_sources": _metric(
            "research_sources", source_text, n, "text", evidence
        ),
        "open_research_count": _metric(
            "open_research_count", len(open_research), len(open_rows), "count", open_evidence
        ),
        "open_research_symbols": _metric(
            "open_research_symbols", open_research, len(open_rows), "text", open_evidence
        ),
        "open_research_sources": _metric(
            "open_research_sources",
            ", ".join(f"{source} {count}" for source, count in sorted(open_sources.items())),
            len(open_rows),
            "text",
            open_evidence,
        ),
    }


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except (TypeError, ValueError) as error:
                    logger.warning("坏 JSONL 行跳过 (%s): %s", path, error, exc_info=True)
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return []
    return rows


def build_shadow_paths(
    report_date: str,
    pullback_path: Path | None = None,
    post_exit_path: Path | None = None,
) -> Dict[str, Metric]:
    """Expose entry path and post-exit giveback from existing shadow artifacts.

    2026-10-20 P0: 日报三问的 "0 chase" 只按 plan/chase cap 判定, 看不到 DA
    确认位层面的漂移; post_exit giveback 也只落在影子文件里。本 section 只读
    `pullback_limit_shadow.jsonl` / `post_exit_path.jsonl`, 不改任何交易路径。
    """
    pullback_rows = [
        row for row in _load_jsonl(pullback_path or _PULLBACK_SHADOW)
        if row.get("date") == report_date and row.get("type") == "trigger"
    ]
    chase = [
        row for row in pullback_rows
        if str(row.get("entry_path") or "") == "chase"
    ]
    pullback = [
        row for row in pullback_rows
        if str(row.get("entry_path") or "") == "pullback"
    ]
    gaps = []
    miss_symbols = []
    for row in _load_jsonl(post_exit_path or _POST_EXIT_SHADOW):
        if row.get("date") != report_date:
            continue
        try:
            gap = float(row.get("close_gap_pct"))
        except (TypeError, ValueError) as error:
            logger.warning("坏 close_gap_pct 行跳过 (%s): %s",
                           row.get("symbol"), error, exc_info=True)
            continue
        gaps.append(gap)
        if gap > 1.0:
            miss_symbols.append(str(row.get("symbol") or "?"))

    entry_n = len(pullback_rows)
    entry_evidence = _derived_evidence(
        "data/shadow/pullback_limit_shadow.jsonl",
        report_date,
        entry_n,
        universe="artifact:shadow",
        note="type=trigger rows; entry_path is direction-aware vs DA confirmation level",
    )
    post_n = len(gaps)
    post_evidence = _derived_evidence(
        "data/shadow/post_exit_path.jsonl",
        report_date,
        post_n,
        universe="artifact:shadow",
        note="close_gap_pct > 0 means holding to close would have been better than the exit",
    )
    return {
        "entry_trigger_count": _metric(
            "entry_trigger_count", entry_n, entry_n, "count", entry_evidence
        ),
        "entry_chase_count": _metric(
            "entry_chase_count", len(chase), entry_n, "count", entry_evidence
        ),
        "entry_pullback_count": _metric(
            "entry_pullback_count", len(pullback), entry_n, "count", entry_evidence
        ),
        "entry_chase_symbols": _metric(
            "entry_chase_symbols", [row.get("symbol") for row in chase],
            entry_n, "text", entry_evidence
        ),
        "entry_pullback_symbols": _metric(
            "entry_pullback_symbols", [row.get("symbol") for row in pullback],
            entry_n, "text", entry_evidence
        ),
        "post_exit_n": _metric("post_exit_n", post_n, post_n, "count", post_evidence),
        "post_exit_close_gap_mean": _metric(
            "post_exit_close_gap_mean",
            round(math.fsum(gaps) / len(gaps), 4) if gaps else 0.0,
            post_n, "pp", post_evidence,
        ),
        "post_exit_close_gap_median": _metric(
            "post_exit_close_gap_median",
            round(statistics.median(gaps), 4) if gaps else 0.0,
            post_n, "pp", post_evidence,
        ),
        "post_exit_miss_gt1_count": _metric(
            "post_exit_miss_gt1_count", len(miss_symbols), post_n, "count", post_evidence
        ),
        "post_exit_miss_symbols": _metric(
            "post_exit_miss_symbols", miss_symbols, post_n, "text", post_evidence
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
            "research_arm": build_research_arm_split(connection, report_date),
            "shadow_paths": build_shadow_paths(report_date),
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
