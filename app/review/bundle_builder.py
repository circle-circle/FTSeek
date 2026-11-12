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
from zoneinfo import ZoneInfo

from app.review.actionability_diagnostics import build_actionability_diagnostics
from app.review.bundle_schema import Bundle, Evidence, Metric
from app.review.decision_diagnostics import build_decision_diagnostics
from app.review.stage_stats import load_trades, stage_breakdown
from app.playbook.instruction_attribution import (
    AMBIGUOUS,
    canonical_plan_direction,
    resolve_trade_instruction_ids,
)
from app.utils.timezone import to_et

logger = logging.getLogger(__name__)

_PULLBACK_SHADOW = (
    Path(__file__).resolve().parents[2] / "data" / "shadow" / "pullback_limit_shadow.jsonl"
)
_POST_EXIT_SHADOW = (
    Path(__file__).resolve().parents[2] / "data" / "shadow" / "post_exit_path.jsonl"
)
_DAILY_KLINE_CACHE = (
    Path(__file__).resolve().parents[2] / "data" / "cache" / "daily_kline_v1.json"
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
    """Compare playbook instructions with strictly attributed playbook trades.

    2026-10-20 P0: 2 分钟门放弃 (`skip_reason='entry_confirm_reversed'`) 必须进
    planned 分母。该行 direction 已被改成 SKIP, 旧查询只看 BUY/SELL_SHORT ⇒ 目标
    从分母消失, 执行率虚高 (10-19 实跑 7/7=100%, 正确口径 7/8=87.5%)。

    口径 (2026-11-12 去重回归修复):
      - `planned_count` / `executed_count` / `missed_count` / `execution_rate` 是
        **计划单元 = 去重后的 (symbol, 计划方向)** 口径, 与 HEAD 一致; 同一标的同向
        多轮指令 (RFC-066 reentry) 只占一个分母, 否则执行率会被轮次稀释。
      - `instruction_count` / `executed_instruction_count` 显式给出**指令行级**读数,
        reentry 轮次由此可见, 两种口径不混用同一个名字。
      - 归属仍严格走 instruction/decision_id (metadata.playbook_instruction_id 优先,
        唯一 decision_id 次之, 不做 symbol 兜底); 归属歧义既不判 executed 也不判
        unplanned, 单列 `attribution_ambiguous_*`。
    """
    instructions = [dict(row) for row in connection.execute(
        "select id, symbol, direction, decision_id, skip_reason, entry_price, "
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
    ).fetchall()]
    instruction_identities = [dict(row) for row in connection.execute(
        "select id, date, symbol, direction, decision_id from playbook_instructions"
    ).fetchall()]
    trades = [dict(row) for row in connection.execute(
        "select id, symbol, direction, trade_date, decision_id, metadata_json from trade_records "
        "where trade_date = ? and source_type in ('PLAYBOOK_SIM', 'PLAYBOOK_LIVE') "
        "and (exit_reason is null or exit_reason != 'PHANTOM_RECONCILE_VOID') "
        "order by id",
        (report_date,),
    ).fetchall()]

    planned_by_id = {int(row["id"]): row for row in instructions}
    planned_ids = [int(row["id"]) for row in instructions]
    # 计划单元: (symbol, 规范方向) 首次出现顺序; 同单元的多行是 reentry 轮次。
    unit_keys: List[tuple] = []
    unit_by_instruction_id: Dict[int, tuple] = {}
    seen_keys = set()
    for row in instructions:
        instruction_id = int(row["id"])
        key = (row["symbol"], canonical_plan_direction(row["direction"]))
        if key not in seen_keys:
            seen_keys.add(key)
            unit_keys.append(key)
        unit_by_instruction_id[instruction_id] = key
    abandoned_ids = {
        int(row["id"]) for row in instructions
        if (row["skip_reason"] or "") == "entry_confirm_reversed"
    }
    abandoned_keys = {
        unit_by_instruction_id[instruction_id] for instruction_id in abandoned_ids
    }
    plan_gap_ids = [
        int(row["id"]) for row in instructions
        if row["gap_pct"] is not None
        and abs(float(row["gap_pct"])) > 1.0
        and str(row["archetype"] or "").upper() in ("PULLBACK_LIMIT", "FADE_SHORT")
    ]
    plan_gap_keys = []
    for instruction_id in plan_gap_ids:
        key = unit_by_instruction_id[instruction_id]
        if key not in plan_gap_keys:
            plan_gap_keys.append(key)

    resolved_ids = resolve_trade_instruction_ids(trades, instruction_identities)
    executed_ids = set()
    executed_keys = set()
    unplanned_trades = []
    ambiguous_trades = []
    direction_mismatch = set()
    for trade, instruction_id in zip(trades, resolved_ids):
        if instruction_id is AMBIGUOUS:
            ambiguous_trades.append(trade)
            continue
        planned = planned_by_id.get(instruction_id)
        actual_direction = canonical_plan_direction(trade.get("direction"))
        if planned is not None:
            planned_direction = canonical_plan_direction(planned.get("direction"))
            if planned_direction == actual_direction:
                executed_ids.add(instruction_id)
                executed_keys.add(unit_by_instruction_id[instruction_id])
                continue
            direction_mismatch.add(
                f"{trade['symbol']}({planned_direction}/{actual_direction})"
            )
        unplanned_trades.append(trade)

    missed_keys = [
        key for key in unit_keys
        if key not in executed_keys and key not in abandoned_keys
    ]
    unplanned_symbols = list(dict.fromkeys(row["symbol"] for row in unplanned_trades))
    ambiguous_symbols = list(dict.fromkeys(row["symbol"] for row in ambiguous_trades))
    sample_n = len(unit_keys)
    evidence = _derived_evidence(
        "playbook_instructions and trade_records strict instruction attribution",
        report_date,
        sample_n,
        universe="db:playbook_instructions+db:trade_records",
        note="plan unit=(symbol,canonical direction) deduped; instruction_count keeps RFC-066 reentry rows; "
             "metadata.playbook_instruction_id first; unique decision_id fallback; no symbol fallback",
    )
    executed_count = len(executed_keys)
    execution_rate = round(executed_count / sample_n, 6) if sample_n else 0.0
    return {
        "plan_available": _metric(
            "plan_available", bool(unit_keys), sample_n, "bool", evidence
        ),
        "planned_count": _metric("planned_count", sample_n, sample_n, "count", evidence),
        "instruction_count": _metric(
            "instruction_count", len(planned_ids), sample_n, "count", evidence
        ),
        "executed_count": _metric(
            "executed_count", executed_count, sample_n, "count", evidence
        ),
        "executed_instruction_count": _metric(
            "executed_instruction_count", len(executed_ids), sample_n, "count", evidence
        ),
        "missed_count": _metric("missed_count", len(missed_keys), sample_n, "count", evidence),
        "abandoned_count": _metric(
            "abandoned_count", len(abandoned_keys), sample_n, "count", evidence
        ),
        "abandoned_symbols": _metric(
            "abandoned_symbols",
            list(dict.fromkeys(
                planned_by_id[row_id]["symbol"] for row_id in planned_ids
                if row_id in abandoned_ids
            )),
            sample_n, "text", evidence
        ),
        "plan_gap_count": _metric(
            "plan_gap_count", len(plan_gap_keys), sample_n, "count", evidence
        ),
        "plan_gap_symbols": _metric(
            "plan_gap_symbols",
            list(dict.fromkeys(key[0] for key in plan_gap_keys)),
            sample_n, "text", evidence
        ),
        "unplanned_count": _metric(
            "unplanned_count", len(unplanned_trades), sample_n, "count", evidence
        ),
        "attribution_ambiguous_count": _metric(
            "attribution_ambiguous_count", len(ambiguous_trades), sample_n, "count", evidence
        ),
        "attribution_ambiguous_symbols": _metric(
            "attribution_ambiguous_symbols", ambiguous_symbols, sample_n, "text", evidence
        ),
        "execution_rate": _metric(
            "execution_rate", execution_rate, sample_n, "ratio", evidence
        ),
        "direction_mismatch": _metric(
            "direction_mismatch", sorted(direction_mismatch), sample_n, "text", evidence
        ),
        "unplanned_symbols": _metric(
            "unplanned_symbols", unplanned_symbols, sample_n, "text", evidence
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


def _regular_close_et(report_date: str) -> datetime:
    return datetime.fromisoformat(f"{report_date}T16:00:00").replace(
        tzinfo=ZoneInfo("America/New_York")
    )


def _exit_at_or_before_regular_close(raw_exit_time: Any, close_dt: datetime) -> bool:
    if not raw_exit_time:
        return False
    try:
        parsed = datetime.fromisoformat(str(raw_exit_time).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=close_dt.tzinfo)
    return parsed.astimezone(close_dt.tzinfo) <= close_dt


def _exact_daily_closes(path: Path, report_date: str) -> Dict[str, float]:
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, TypeError, ValueError):
        return {}
    closes = {}
    for symbol, symbol_payload in (payload.get("symbols") or {}).items():
        bars = symbol_payload.get("bars") or []
        exact = next((bar for bar in reversed(bars) if bar.get("time") == report_date), None)
        try:
            close = float(exact.get("close")) if exact else 0.0
        except (TypeError, ValueError):
            close = 0.0
        if close > 0:
            closes[symbol] = close
    return closes


def build_da_cohort_risk(
    connection: sqlite3.Connection,
    report_date: str,
    daily_kline_path: Path | None = None,
) -> Dict[str, Any]:
    """Separate new-DA realized PnL from regular-close open MTM; never mix into A9."""
    rows = [dict(row) for row in connection.execute(
        "select id, symbol, direction, decision_id, entry_price, exit_time, quantity, "
        "realized_pnl, pnl_net, fees_est, metadata_json from trade_records "
        "where trade_date = ? and decision_id like 'DA-%' "
        "and source_type in ('PLAYBOOK_SIM', 'PLAYBOOK_LIVE') "
        "and (exit_reason is null or exit_reason != 'PHANTOM_RECONCILE_VOID') "
        "and coalesce(data_quality, '') <> 'duplicate_external_close' order by id",
        (report_date,),
    ).fetchall()]
    close_dt = _regular_close_et(report_date)
    closed = []
    open_at_close = []
    for row in rows:
        if _exit_at_or_before_regular_close(row.get("exit_time"), close_dt):
            closed.append(row)
        else:
            open_at_close.append(row)

    realized_values = [row.get("pnl_net") for row in closed]
    realized_complete = all(value is not None for value in realized_values)
    realized_net = (
        round(math.fsum(float(value) for value in realized_values), 2)
        if realized_complete else None
    )
    gross_values = [row.get("realized_pnl") for row in closed]
    fee_values = [row.get("fees_est") for row in closed]
    gross_complete = all(value is not None for value in gross_values)
    fees_complete = all(value is not None for value in fee_values)
    realized_gross = (
        round(math.fsum(float(value) for value in gross_values), 2)
        if gross_complete else None
    )
    realized_fees = (
        round(math.fsum(float(value) for value in fee_values), 2)
        if fees_complete else None
    )
    fee_to_gross = (
        round(realized_fees / abs(realized_gross), 6)
        if realized_fees is not None and realized_gross else None
    )
    closes = _exact_daily_closes(
        daily_kline_path or _DAILY_KLINE_CACHE, report_date
    ) if open_at_close else {}
    position_rows = []
    open_gross_values = []
    missing_symbols = []
    for row in open_at_close:
        metadata = _parse_metadata(row.get("metadata_json"))
        symbol = row["symbol"]
        close = closes.get(symbol)
        try:
            entry = float(row.get("entry_price") or 0.0)
            quantity = float(row.get("quantity") or 0.0)
        except (TypeError, ValueError):
            entry = quantity = 0.0
        gross_mtm = None
        if close is not None and entry > 0 and quantity > 0:
            sign = 1.0 if canonical_plan_direction(row.get("direction")) == "BUY" else -1.0
            gross_mtm = round(sign * (close - entry) * quantity, 2)
            open_gross_values.append(gross_mtm)
        else:
            missing_symbols.append(symbol)
        observed_stop = metadata.get("_effective_sl_observed")
        initial_stop = metadata.get("stop_loss")
        effective_stop = observed_stop if isinstance(observed_stop, (int, float)) else initial_stop
        position_rows.append({
            "symbol": symbol,
            "direction": row.get("direction"),
            "decision_id": row.get("decision_id"),
            "entry_price": entry or None,
            "close_price": close,
            "quantity": quantity or None,
            "gross_mtm": gross_mtm,
            "effective_stop": effective_stop,
            "effective_stop_source": (
                "observed" if isinstance(observed_stop, (int, float)) else "initial"
            ),
        })
    open_complete = not missing_symbols
    open_gross_mtm = round(math.fsum(open_gross_values), 2) if open_complete else None
    evidence = _derived_evidence(
        "trade_records new-DA cohort + exact-date daily_kline_v1 regular close",
        report_date,
        len(rows),
        universe="db:trade_records+artifact:daily_kline_v1",
        note="realized=exits at/before 16:00 ET with pnl_net; open MTM=gross, excludes fees, never OKR A9",
    )
    return {
        "cohort": _metric("cohort", "new_da_trade_date", len(rows), "text", evidence),
        "realized_trade_count": _metric(
            "realized_trade_count", len(closed), len(rows), "count", evidence
        ),
        "realized_net_pnl": _metric(
            "realized_net_pnl", realized_net, len(rows), "usd", evidence
        ),
        "realized_net_complete": _metric(
            "realized_net_complete", realized_complete, len(rows), "bool", evidence
        ),
        "realized_gross_pnl": _metric(
            "realized_gross_pnl", realized_gross, len(rows), "usd", evidence
        ),
        "realized_fees": _metric(
            "realized_fees", realized_fees, len(rows), "usd", evidence
        ),
        "realized_fee_to_gross": _metric(
            "realized_fee_to_gross", fee_to_gross, len(rows), "ratio", evidence
        ),
        "open_trade_count": _metric(
            "open_trade_count", len(open_at_close), len(rows), "count", evidence
        ),
        "open_gross_mtm": _metric(
            "open_gross_mtm", open_gross_mtm, len(rows), "usd", evidence
        ),
        "open_mtm_complete": _metric(
            "open_mtm_complete", open_complete, len(rows), "bool", evidence
        ),
        "open_mtm_missing_symbols": _metric(
            "open_mtm_missing_symbols", missing_symbols, len(rows), "text", evidence
        ),
        "mtm_as_of": _metric(
            "mtm_as_of", close_dt.isoformat(), len(rows), "text", evidence
        ),
        "positions": position_rows,
    }


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
    connection: sqlite3.Connection | None = None,
) -> Dict[str, Any]:
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
    valid_post_rows = []
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
        valid_post_rows.append((row, gap))
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

    channel_rows = {"DA": [], "EXT": []}
    if connection is not None and valid_post_rows:
        trade_ids = [row.get("trade_id") for row, _ in valid_post_rows if row.get("trade_id") is not None]
        if trade_ids:
            placeholders = ",".join("?" for _ in trade_ids)
            decisions = {
                int(row["id"]): str(row["decision_id"] or "")
                for row in connection.execute(
                    f"select id, decision_id from trade_records where id in ({placeholders})",
                    trade_ids,
                ).fetchall()
            }
            for row, gap in valid_post_rows:
                decision_id = decisions.get(int(row.get("trade_id") or 0), "")
                channel = "DA" if decision_id.startswith("DA-") else (
                    "EXT" if decision_id.startswith("EXT-") else ""
                )
                if channel:
                    channel_rows[channel].append((row, gap))

    def _channel_section(channel: str) -> Dict[str, Metric]:
        rows = channel_rows[channel]
        channel_gaps = [gap for _, gap in rows]
        symbols = [str(row.get("symbol") or "?") for row, gap in rows if gap > 1.0]
        evidence = _derived_evidence(
            "post_exit_path joined to trade_records.decision_id",
            report_date,
            len(rows),
            universe=f"artifact:shadow+db:trade_records;channel={channel}",
            note="observed post-exit path only; not counterfactual exit-rule PnL",
        )
        return {
            "n": _metric(f"{channel.lower()}_post_exit_n", len(rows), len(rows), "count", evidence),
            "close_gap_mean": _metric(
                f"{channel.lower()}_post_exit_close_gap_mean",
                round(math.fsum(channel_gaps) / len(channel_gaps), 4) if channel_gaps else 0.0,
                len(rows), "pp", evidence,
            ),
            "close_gap_median": _metric(
                f"{channel.lower()}_post_exit_close_gap_median",
                round(statistics.median(channel_gaps), 4) if channel_gaps else 0.0,
                len(rows), "pp", evidence,
            ),
            "miss_gt1_count": _metric(
                f"{channel.lower()}_post_exit_miss_gt1_count", len(symbols), len(rows), "count", evidence
            ),
            "miss_symbols": _metric(
                f"{channel.lower()}_post_exit_miss_symbols", symbols, len(rows), "text", evidence
            ),
        }

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
        "da_post_exit": _channel_section("DA"),
        "ext_post_exit": _channel_section("EXT"),
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
            "da_cohort_risk": build_da_cohort_risk(connection, report_date),
            "research_arm": build_research_arm_split(connection, report_date),
            "shadow_paths": build_shadow_paths(report_date, connection=connection),
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
