#!/usr/bin/env python3
"""Original Playbook profit-protection decisions for extended-hours exits."""
import json
import sqlite3
from pathlib import Path


PROFIT_EXIT_REASONS = {"TAKE_PROFIT", "SL_BREAKEVEN", "SL_TRAIL_LOCK"}


def _number(value, default=0.0):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed


def parse_trade_metadata(value):
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def decide_original_profit_exit(
    direction,
    entry_price,
    executable_price,
    initial_sl,
    peak_price,
    trade_metadata,
    fee_pct,
    source_type=None,
):
    """Apply the original TP/BE/trail-lock rules to an executable quote.

    ``SL_INITIAL`` is deliberately converted to ``hold_no_stop``. The ``SL_``
    prefix on BE/TRAIL_LOCK is historical: once armed they protect realized
    profit, while SL_INITIAL is the intraday-only loss stop.
    """
    direction = str(direction or "LONG").upper()
    entry = _number(entry_price)
    current = _number(executable_price)
    metadata = parse_trade_metadata(trade_metadata)
    if entry <= 0 or current <= 0:
        return {"action": "no_data", "metadata": metadata}

    sign = -1 if direction in {"SHORT", "SELL_SHORT", "SELL"} else 1
    initial_sl = _number(metadata.get("stop_loss", initial_sl))
    old_peak = _number(peak_price or metadata.get("peak_price"), entry)
    peak = old_peak if old_peak > 0 else entry
    if direction in {"SHORT", "SELL_SHORT", "SELL"}:
        peak = current if peak <= 0 or current < peak else peak
    else:
        peak = max(peak, current)

    profit_pct = (current - entry) / entry * 100 * sign
    peak_profit_pct = (peak - entry) / entry * 100 * sign

    from app.playbook.config import get_playbook_config
    from app.playbook.adaptive_trailing import (
        adaptive_trailing_drawdown_pct,
        adaptive_trailing_snapshot,
    )
    from app.playbook.exit_engine import (
        _compute_dynamic_effective_sl,
        _get_factor_exit_config,
    )

    playbook_config = get_playbook_config()
    factor_rules = playbook_config.get_horizon_top("factor_exit_rules", {})
    exit_config = _get_factor_exit_config(
        metadata.get("conditions", []), factor_rules, direction=direction
    )
    if fee_pct is not None:
        fee_cushion_pct = _number(fee_pct)
    else:
        fee_cushion_pct = _number(
            metadata.get("_be_cushion_fee_aware", 0.15)
        )
    slip_cushion_pct = _number(
        playbook_config.get("breakeven_slip_cushion_pct", 0.0)
    )
    source_type = str(source_type or "").strip().upper()
    adaptive_drawdown_pct = adaptive_trailing_drawdown_pct(
        peak_profit_pct, playbook_config, source_type
    )
    adaptive_trailing_snapshot_value = None
    if adaptive_drawdown_pct is not None:
        adaptive_trailing_snapshot_value = adaptive_trailing_snapshot(
            peak_profit_pct, playbook_config, source_type
        )
        metadata["_adaptive_trailing"] = adaptive_trailing_snapshot_value

    metadata["peak_price"] = round(peak, 4)
    metadata["_be_cushion_fee_aware"] = round(fee_cushion_pct, 4)
    metadata["_be_net_line_price"] = round(
        entry * (1 + sign * fee_cushion_pct / 100.0), 4
    )

    effective_sl = _compute_dynamic_effective_sl(
        direction,
        entry,
        initial_sl,
        peak_profit_pct,
        exit_config,
        fee_cushion_pct,
        slip_cushion_pct,
        adaptive_drawdown_pct,
    )
    breakeven_at_pct = _number(exit_config.get("breakeven_at_pct"), 0.5)
    breakeven_triggered = bool(
        metadata.get("breakeven_triggered")
        or peak_profit_pct >= breakeven_at_pct
    )
    metadata["breakeven_triggered"] = breakeven_triggered

    long_position = sign > 0
    protective_line_breached = (
        current <= effective_sl
        if long_position
        else current >= effective_sl
    )
    be_line = entry * (1 + sign * (fee_cushion_pct + slip_cushion_pct) / 100.0)
    be_tolerance = entry * 1e-4
    action = "hold"
    detail = ""
    take_profit = 0.0
    tp_rr = None

    if protective_line_breached:
        if not breakeven_triggered:
            action = "hold_no_stop"
            detail = "initial_loss_stop_deferred_to_rth"
        elif (long_position and effective_sl > be_line + be_tolerance) or (
            not long_position and 0 < effective_sl < be_line - be_tolerance
        ):
            action = "SL_TRAIL_LOCK"
            detail = "locked_profit_floor_breached"
        else:
            action = "SL_BREAKEVEN"
            detail = "net_breakeven_floor_breached"
    else:
        take_profit = _number(metadata.get("take_profit"))
        min_tp_rr = _number(playbook_config.get("min_tp_rr"), 1.5)
        tp_risk = abs(entry - initial_sl) if initial_sl > 0 else 0.0
        tp_reward = abs(take_profit - entry)
        tp_rr = tp_reward / tp_risk if tp_risk > 0 else 999.0
        take_profit_hit = (
            current >= take_profit if long_position else current <= take_profit
        )
        if take_profit > 0 and take_profit_hit:
            if tp_rr < min_tp_rr:
                action = "hold"
                detail = "take_profit_rr_gate"
            elif profit_pct - fee_cushion_pct <= 0:
                action = "hold"
                detail = "take_profit_below_net_fee"
            else:
                action = "TAKE_PROFIT"
                detail = "take_profit_line_reached"
        elif profit_pct < 0:
            action = "hold_no_stop"
            detail = "underwater_no_extended_loss_stop"

    return {
        "action": action,
        "detail": detail,
        "metadata": metadata,
        "peak_price": round(peak, 4),
        "profit_pct": round(profit_pct, 4),
        "net_profit_pct": round(profit_pct - fee_cushion_pct, 4),
        "peak_profit_pct": round(peak_profit_pct, 4),
        "effective_sl": round(effective_sl, 4),
        "breakeven_triggered": breakeven_triggered,
        "take_profit_rr": round(tp_rr, 6) if take_profit > 0 else None,
        "adaptive_trailing_drawdown_pct": adaptive_drawdown_pct,
        "adaptive_trailing_snapshot": adaptive_trailing_snapshot_value,
    }


def persist_profit_exit_state(db_path, trade_id, decision, direction):
    """Merge peak/BE state into trade metadata without overwriting rivals."""
    trade_id = int(trade_id or 0)
    if trade_id <= 0:
        return False

    conn = sqlite3.connect(Path(db_path), timeout=30)
    try:
        row = conn.execute(
            "SELECT metadata_json FROM trade_records WHERE id=?", (trade_id,)
        ).fetchone()
        if row is None:
            return False

        metadata = parse_trade_metadata(row[0])
        old_peak = _number(metadata.get("peak_price"), 0.0)
        new_peak = _number(decision.get("peak_price"), 0.0)
        short = str(direction or "").upper() in {"SHORT", "SELL_SHORT", "SELL"}
        peak_changed = (
            new_peak < old_peak
            if short and old_peak > 0
            else new_peak > old_peak
        )
        if peak_changed:
            metadata["peak_price"] = round(new_peak, 4)

        if decision.get("breakeven_triggered"):
            metadata["breakeven_triggered"] = True

        updated_metadata = decision.get("metadata") or {}
        for key in ("_be_cushion_fee_aware", "_be_net_line_price",
                    "_adaptive_trailing"):
            if key in updated_metadata:
                metadata[key] = updated_metadata[key]

        updated_json = json.dumps(metadata, ensure_ascii=False)
        if updated_json == row[0]:
            return False
        conn.execute(
            "UPDATE trade_records SET metadata_json=? WHERE id=?",
            (updated_json, trade_id),
        )
        conn.commit()
        return True
    finally:
        conn.close()
