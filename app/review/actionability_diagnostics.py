"""Read-only actionability diagnostics for the daily review bundle."""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from app.review.bundle_schema import Evidence, Metric
from app.utils.timezone import to_et


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BARS_DIR = ROOT / "data" / "replay_bars"
MIN_ACTIONABLE_NET = 50.0
DELAY_MINUTES = 5

_log = logging.getLogger(__name__)


def _derived_evidence(
    ref: str,
    report_date: str,
    sample_n: int,
    universe: str,
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


def _load_json(value: Any) -> Dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _parse_et_time(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return to_et(parsed).replace(tzinfo=None)


def _parse_bar_time(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed
    return to_et(parsed).replace(tzinfo=None)


def _exit_date_et(value: Any) -> str:
    parsed = _parse_et_time(value)
    return parsed.date().isoformat() if parsed else ""


def _available_columns(connection: sqlite3.Connection, table: str) -> set:
    return {row[1] for row in connection.execute(f"pragma table_info({table})")}


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "select 1 from sqlite_master where type='table' and name=?", (table,)
    ).fetchone()
    return row is not None


def _load_closed_trades(
    connection: sqlite3.Connection, report_date: str
) -> List[Dict[str, Any]]:
    wanted_columns = [
        "id", "symbol", "direction", "entry_time", "exit_time", "entry_price",
        "exit_price", "entry_session", "exit_reason", "quantity", "pnl_net",
        "fees_est", "mfe_pct", "mae_pct", "data_quality", "metadata_json",
    ]
    available = _available_columns(connection, "trade_records")
    selected = [column for column in wanted_columns if column in available]
    clauses = ["exit_time is not null", "pnl_net is not null"]
    if "data_quality" in available:
        clauses.append("coalesce(data_quality, '') <> 'duplicate_external_close'")
    rows = connection.execute(
        f"select {', '.join(selected)} from trade_records "
        f"where {' and '.join(clauses)} order by exit_time, id"
    ).fetchall()
    result = []
    for row in rows:
        trade = dict(row) if not isinstance(row, sqlite3.Row) else {
            key: row[key] for key in row.keys()
        }
        if _exit_date_et(trade.get("exit_time")) == report_date:
            trade["metadata"] = _load_json(trade.get("metadata_json"))
            result.append(trade)
    return result


def _bars_path(symbol: str, report_date: str, bars_dir: Optional[Path]) -> Path:
    directory = Path(bars_dir) if bars_dir is not None else DEFAULT_BARS_DIR
    sanitized = str(symbol).replace(".", "_").replace("/", "_").replace("\\", "_")
    return directory / f"{sanitized}_{report_date}.json"


def _load_bars(
    symbol: str, report_date: str, bars_dir: Optional[Path]
) -> List[Dict[str, Any]]:
    path = _bars_path(symbol, report_date, bars_dir)
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(payload, list):
        return []
    bars = []
    for item in payload:
        if not isinstance(item, (list, tuple)) or len(item) < 5:
            continue
        timestamp = _parse_bar_time(item[0])
        if timestamp is None:
            continue
        try:
            values = [float(item[1]), float(item[2]), float(item[3]), float(item[4])]
        except (TypeError, ValueError):
            _log.warning("bar parse failed", exc_info=True)
            continue
        bars.append(
            {
                "time": timestamp,
                "open": values[0],
                "high": max(values),
                "low": min(values),
                "close": values[3],
            }
        )
    return bars


def _direction_sign(direction: Any) -> int:
    return 1 if str(direction or "").upper() in ("LONG", "BUY") else -1


def _window_bars(trade: Mapping[str, Any], bars: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    entry_time = _parse_et_time(trade.get("entry_time"))
    exit_time = _parse_et_time(trade.get("exit_time"))
    if entry_time is None or exit_time is None:
        return []
    return [dict(bar) for bar in bars if entry_time <= bar["time"] <= exit_time]


def _rescue_line(trade: Mapping[str, Any]) -> Tuple[Optional[float], Optional[str]]:
    metadata = trade.get("metadata") or {}
    breakeven = metadata.get("_be_net_line_price")
    if isinstance(breakeven, (int, float)):
        return float(breakeven), "breakeven_lock"
    if isinstance(metadata.get("_adaptive_trailing"), dict):
        effective_stop = metadata.get("_effective_sl_at_exit")
        if isinstance(effective_stop, (int, float)):
            return float(effective_stop), "trailing_lock"
    effective_stop = metadata.get("_effective_sl_at_exit")
    if isinstance(effective_stop, (int, float)):
        return float(effective_stop), "tightened_stop"
    return None, None


def _favorable_price(bar: Mapping[str, Any], direction_sign: int) -> float:
    return float(bar["high"]) if direction_sign == 1 else float(bar["low"])


def _adverse_price(bar: Mapping[str, Any], direction_sign: int) -> float:
    return float(bar["low"]) if direction_sign == 1 else float(bar["high"])


def _persistent_favorable_run(
    bars: List[Mapping[str, Any]], direction_sign: int, line: float
) -> Tuple[int, int]:
    longest = 0
    current = 0
    start_index = -1
    longest_start = -1
    for index, bar in enumerate(bars):
        favorable = _favorable_price(bar, direction_sign)
        reached = favorable >= line if direction_sign == 1 else favorable <= line
        if reached:
            if current == 0:
                start_index = index
            current += 1
            if current > longest:
                longest = current
                longest_start = start_index
        else:
            current = 0
    return longest, longest_start


def _rescue_exit_price(
    bars: List[Mapping[str, Any]], direction_sign: int, line: float, trigger_index: int,
    actual_exit: Optional[float],
) -> float:
    for bar in bars[trigger_index:]:
        adverse = _adverse_price(bar, direction_sign)
        hit_line = adverse <= line if direction_sign == 1 else adverse >= line
        if hit_line:
            return line
    return float(actual_exit or line)


def _net(
    entry_price: Optional[float], exit_price: Optional[float], quantity: Any,
    fees: Any, direction_sign: int, slippage_bps: Any = 0,
) -> Optional[float]:
    if entry_price is None or exit_price is None:
        return None
    try:
        shares = float(quantity or 0)
        costs = float(fees or 0)
        slip_bps = abs(float(slippage_bps or 0))
    except (TypeError, ValueError):
        return None
    costs += abs(float(exit_price)) * slip_bps / 10_000.0 * shares
    gross = (exit_price - entry_price) * shares if direction_sign == 1 else (
        entry_price - exit_price
    ) * shares
    return round(gross - costs, 2)


def _build_actionable_mfe(
    connection: sqlite3.Connection, report_date: str,
    trades: List[Dict[str, Any]], bars_dir: Optional[Path],
) -> Dict[str, Metric]:
    evaluated = 0
    unavailable = 0
    cases = []
    for trade in trades:
        bars = _load_bars(trade["symbol"], report_date, bars_dir)
        window = _window_bars(trade, bars)
        if not window:
            unavailable += 1
            continue
        evaluated += 1
        direction_sign = _direction_sign(trade.get("direction"))
        line, rescue_action = _rescue_line(trade)
        if line is None or rescue_action is None:
            continue
        persistent, trigger_index = _persistent_favorable_run(
            window, direction_sign, line
        )
        if persistent < 2 or trigger_index < 0:
            continue
        rescue_exit = _rescue_exit_price(
            window, direction_sign, line, trigger_index, trade.get("exit_price")
        )
        estimated_net = _net(
            trade.get("entry_price"), rescue_exit, trade.get("quantity"),
            trade.get("fees_est"), direction_sign,
            (trade.get("metadata") or {}).get("exit_slippage_bps"),
        )
        if estimated_net is None or estimated_net < MIN_ACTIONABLE_NET:
            continue
        cases.append(
            {
                "trade_id": trade.get("id"),
                "symbol": trade.get("symbol"),
                "direction": trade.get("direction"),
                "persistent_bars": persistent,
                "rescue_action": rescue_action,
                "rescue_exit_price": round(rescue_exit, 6),
                "estimated_rescue_net": estimated_net,
                "verdict": "candidate_not_verdict",
            }
        )
    evidence = _derived_evidence(
        "persistent favorable bars + pre-registered rescue-line replay",
        report_date,
        len(trades),
        "db:trade_records+file:replay_bars",
    )
    return {
        "evaluated_count": _metric("evaluated_count", evaluated, len(trades), "count", evidence),
        "actionable_count": _metric(
            "actionable_count", len(cases), len(trades), "count", evidence
        ),
        "bar_unavailable_count": _metric(
            "bar_unavailable_count", unavailable, len(trades), "count", evidence
        ),
        "cases": _metric("cases", cases, len(trades), "text", evidence),
    }


def _build_regret_price(
    connection: sqlite3.Connection, report_date: str,
    trades: List[Dict[str, Any]], bars_dir: Optional[Path],
) -> Dict[str, Metric]:
    evaluated = 0
    unavailable = 0
    profitable = 0
    cases = []
    for trade in trades:
        bars = _load_bars(trade["symbol"], report_date, bars_dir)
        window = _window_bars(trade, bars)
        if not window:
            unavailable += 1
            continue
        evaluated += 1
        direction_sign = _direction_sign(trade.get("direction"))
        line, rescue_action = _rescue_line(trade)
        actual_exit = trade.get("exit_price")
        effective_action = "actual_exit"
        rescue_exit = float(actual_exit) if actual_exit is not None else None
        if line is not None and actual_exit is not None:
            persistent, trigger_index = _persistent_favorable_run(
                window, direction_sign, line
            )
            if persistent >= 2 and trigger_index >= 0:
                rescue_exit = _rescue_exit_price(
                    window, direction_sign, line, trigger_index, actual_exit
                )
                effective_action = rescue_action or effective_action
        if rescue_exit is None:
            cases.append(
                {
                    "trade_id": trade.get("id"), "symbol": trade.get("symbol"),
                    "regret_price": None, "estimated_net_at_regret": None,
                    "status": "no_rescue_or_exit_price", "verdict": "candidate_not_verdict",
                }
            )
            continue
        best_price = None
        best_net = None
        for bar in window:
            candidate = bar["low"] if direction_sign == 1 else bar["high"]
            candidate_net = _net(
                candidate, rescue_exit, trade.get("quantity"), trade.get("fees_est"),
                direction_sign, (trade.get("metadata") or {}).get("exit_slippage_bps"),
            )
            if candidate_net is None or candidate_net <= 0:
                continue
            if (
                best_price is None
                or (direction_sign == 1 and candidate > best_price)
                or (direction_sign == -1 and candidate > best_price)
            ):
                best_price = candidate
                best_net = candidate_net
        if best_price is not None:
            profitable += 1
        entry_price = trade.get("entry_price")
        gap = (
            round((best_price - entry_price) / entry_price, 6)
            if best_price is not None and entry_price
            else None
        )
        cases.append(
            {
                "trade_id": trade.get("id"), "symbol": trade.get("symbol"),
                "direction": trade.get("direction"),
                "rescue_action": effective_action,
                "regret_price": round(best_price, 6) if best_price is not None else None,
                "regret_gap_pct": gap,
                "estimated_net_at_regret": best_net,
                "status": "profitable_limit_candidate" if best_price is not None else "no_profitable_limit_candidate",
                "verdict": "candidate_not_verdict",
            }
        )
    evidence = _derived_evidence(
        "highest fillable bar price with positive net under rescue/actual exit",
        report_date, len(trades), "db:trade_records+file:replay_bars",
    )
    return {
        "evaluated_count": _metric("evaluated_count", evaluated, len(trades), "count", evidence),
        "profitable_candidate_count": _metric(
            "profitable_candidate_count", profitable, len(trades), "count", evidence
        ),
        "bar_unavailable_count": _metric(
            "bar_unavailable_count", unavailable, len(trades), "count", evidence
        ),
        "cases": _metric("cases", cases, len(trades), "text", evidence),
    }


def _build_delay_5(
    connection: sqlite3.Connection, report_date: str,
    trades: List[Dict[str, Any]], bars_dir: Optional[Path],
) -> Dict[str, Metric]:
    evaluated = 0
    unavailable = 0
    actual_better = 0
    delayed_better = 0
    neutral = 0
    cases = []
    for trade in trades:
        bars = _load_bars(trade["symbol"], report_date, bars_dir)
        entry_time = _parse_et_time(trade.get("entry_time"))
        if not bars or entry_time is None:
            unavailable += 1
            continue
        target = entry_time + timedelta(minutes=DELAY_MINUTES)
        delayed_bar = None
        minimum_distance = None
        for bar in bars:
            distance = abs((bar["time"] - target).total_seconds())
            if distance > 60:
                continue
            if minimum_distance is None or distance < minimum_distance:
                delayed_bar = bar
                minimum_distance = distance
        if delayed_bar is None:
            unavailable += 1
            continue
        evaluated += 1
        delayed_net = _net(
            delayed_bar["close"], trade.get("exit_price"), trade.get("quantity"),
            trade.get("fees_est"), _direction_sign(trade.get("direction")),
            (trade.get("metadata") or {}).get("exit_slippage_bps"),
        )
        actual_net = float(trade.get("pnl_net") or 0.0)
        if delayed_net is None:
            unavailable += 1
            evaluated -= 1
            continue
        difference = round(actual_net - delayed_net, 2)
        if difference > 0:
            actual_better += 1
        elif difference < 0:
            delayed_better += 1
        else:
            neutral += 1
        entry_price = trade.get("entry_price")
        delayed_gap = (
            round((delayed_bar["close"] - entry_price) / entry_price, 6)
            if entry_price else None
        )
        cases.append(
            {
                "trade_id": trade.get("id"), "symbol": trade.get("symbol"),
                "direction": trade.get("direction"),
                "delayed_entry_price": delayed_bar["close"],
                "delayed_entry_gap_pct": delayed_gap,
                "delayed_net": delayed_net,
                "actual_net": round(actual_net, 2),
                "actual_minus_delayed": difference,
                "result": "actual_better" if difference > 0 else "delayed_better" if difference < 0 else "neutral",
                "verdict": "candidate_not_verdict",
            }
        )
    evidence = _derived_evidence(
        "same exit/fees/quantity with entry delayed by five one-minute bars",
        report_date, len(trades), "db:trade_records+file:replay_bars",
    )
    return {
        "evaluated_count": _metric("evaluated_count", evaluated, len(trades), "count", evidence),
        "actual_better_count": _metric(
            "actual_better_count", actual_better, len(trades), "count", evidence
        ),
        "delayed_better_count": _metric(
            "delayed_better_count", delayed_better, len(trades), "count", evidence
        ),
        "neutral_count": _metric("neutral_count", neutral, len(trades), "count", evidence),
        "bar_unavailable_count": _metric(
            "bar_unavailable_count", unavailable, len(trades), "count", evidence
        ),
        "cases": _metric("cases", cases, len(trades), "text", evidence),
    }


def _first_fetched_at(context: Mapping[str, Any]) -> Optional[str]:
    raw_data = _load_json(context.get("raw_data_json"))
    for value in raw_data.values():
        if isinstance(value, dict):
            fetched_at = value.get("fetched_at") or value.get("timestamp")
            if fetched_at:
                return str(fetched_at)
    created_at = context.get("created_at")
    return str(created_at) if created_at else None


def _build_pretrade_card_coverage(
    connection: sqlite3.Connection, report_date: str
) -> Dict[str, Metric]:
    if not _table_exists(connection, "playbook_instructions"):
        instructions = []
    else:
        wanted_columns = [
            "date", "symbol", "direction", "thesis", "invalidation",
            "raw_instruction_json", "metadata_json", "decision_id", "config_version",
            "model_used", "decision_conf", "confidence",
        ]
        available_columns = _available_columns(connection, "playbook_instructions")
        selected_columns = [
            column for column in wanted_columns if column in available_columns
        ]
        instruction_rows = connection.execute(
            f"select {', '.join(selected_columns)} "
            "from playbook_instructions where date=? order by id",
            (report_date,),
        ).fetchall()
        instructions = [
            dict(row) if not isinstance(row, sqlite3.Row) else {
                key: row[key] for key in row.keys()
            }
            for row in instruction_rows
        ]
    context_map = {}
    if _table_exists(connection, "playbook_signal_context"):
        context_rows = connection.execute(
            "select date, symbol, raw_data_json, created_at "
            "from playbook_signal_context where date=? order by symbol",
            (report_date,),
        ).fetchall()
        context_map = {row["symbol"]: dict(row) for row in context_rows}
    cases = []
    complete_count = 0
    for instruction in instructions:
        raw = _load_json(instruction["raw_instruction_json"])
        metadata = _load_json(instruction["metadata_json"])
        context = context_map.get(instruction["symbol"], {})
        primary = (
            raw.get("prompt_primary_class")
            or raw.get("strategy_type")
            or raw.get("entry_archetype")
        )
        secondary = raw.get("prompt_secondary_class") or raw.get("secondary_strategy")
        prompt_version = raw.get("prompt_version") or raw.get("prompt_id")
        data_source_time = _first_fetched_at(context)
        candidate_top5 = (
            raw.get("candidate_top_5") or raw.get("candidates") or raw.get("top5")
            or metadata.get("candidate_top_5")
        )
        rejection_reasons = (
            raw.get("rejection_reasons") or raw.get("rejections")
            or metadata.get("rejection_reasons")
        )
        scenarios = raw.get("scenarios") or metadata.get("scenarios")
        if not isinstance(scenarios, dict):
            scenarios = {}
        rescue_actions = (
            raw.get("rescue_actions") or raw.get("pre_registered_rescue_actions")
            or metadata.get("rescue_actions")
        )
        checks = {
            "prompt_primary_class": bool(primary),
            "prompt_secondary_class": bool(secondary),
            "prompt_confidence": (
                instruction.get("decision_conf") is not None
                or instruction.get("confidence") is not None
            ),
            "prompt_version": bool(prompt_version),
            "config_version": bool(instruction.get("config_version")),
            "data_source_time": bool(data_source_time),
            "candidate_top5": bool(candidate_top5),
            "rejection_reasons": bool(rejection_reasons),
            "selected_symbol_rationale": bool(
                instruction.get("thesis") or raw.get("da_thesis")
            ),
            "scenario_trend_continuation": bool(scenarios.get("trend_continuation")),
            "scenario_spike_and_fail": bool(scenarios.get("spike_and_fail")),
            "scenario_direct_failure": bool(scenarios.get("direct_failure")),
            "scenario_low_vol_sideways": bool(scenarios.get("low_vol_sideways")),
            "invalidation": bool(
                instruction.get("invalidation") or raw.get("invalidation")
            ),
            "rescue_actions": bool(rescue_actions),
        }
        missing = [field for field, present in checks.items() if not present]
        if not missing:
            complete_count += 1
        cases.append(
            {
                "decision_id": instruction.get("decision_id"),
                "symbol": instruction.get("symbol"),
                "direction": instruction.get("direction"),
                "missing_fields": missing,
                "complete": not missing,
                "verdict": "coverage_audit_not_verdict",
            }
        )
    evidence = _derived_evidence(
        "required pretrade-card field coverage audit",
        report_date, len(instructions),
        "db:playbook_instructions+db:playbook_signal_context",
    )
    coverage_ratio = round(complete_count / len(instructions), 6) if instructions else 0.0
    return {
        "instruction_count": _metric(
            "instruction_count", len(instructions), len(instructions), "count", evidence
        ),
        "complete_card_count": _metric(
            "complete_card_count", complete_count, len(instructions), "count", evidence
        ),
        "coverage_ratio": _metric(
            "coverage_ratio", coverage_ratio, len(instructions), "ratio", evidence
        ),
        "cases": _metric("cases", cases, len(instructions), "text", evidence),
    }


def build_actionability_diagnostics(
    connection: sqlite3.Connection,
    report_date: str,
    replay_bars_dir: Optional[Path] = None,
) -> Dict[str, Dict[str, Metric]]:
    trades = _load_closed_trades(connection, report_date)
    return {
        "actionable_mfe": _build_actionable_mfe(
            connection, report_date, trades, replay_bars_dir
        ),
        "regret_price": _build_regret_price(
            connection, report_date, trades, replay_bars_dir
        ),
        "delay_5": _build_delay_5(connection, report_date, trades, replay_bars_dir),
        "pretrade_card": _build_pretrade_card_coverage(connection, report_date),
    }
