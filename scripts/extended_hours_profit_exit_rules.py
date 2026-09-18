#!/usr/bin/env python3
"""Original Playbook profit-protection decisions for extended-hours exits."""
import json
import os
import sqlite3
from pathlib import Path


PROFIT_EXIT_REASONS = {"TAKE_PROFIT", "SL_BREAKEVEN", "SL_TRAIL_LOCK", "SL_CERT_LOCK"}


def prioritize_profit_exit(action, fallback=None):
    """Return an extended-hours action without letting a fallback override profit protection."""
    action = str(action or "")
    if action in PROFIT_EXIT_REASONS:
        return action
    return fallback if fallback is not None else action


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

    # 2026-09-19 (用户令「峰值 high 必须是入场之后的」): 影子从**进场后**的 1m bar 取真实
    #   高点写入 metadata.session_high_post_entry (见 market_session.post_entry_high, 已按
    #   bar 时间戳丢弃进场前高点)。开关 EXT_PEAK_USE_SESSION_HIGH=1 (默认关 ⇒ 行为不变)。
    #   回放依据: 配对 +0.381pp/笔 (18好/20平/8差); 真实时段高点比可成交峰值平均高 1.192pp
    #   ⇒ 现役地板被系统性压低。回滚: 去掉 env。
    if os.getenv("EXT_PEAK_USE_SESSION_HIGH", "0").lower() in {"1", "true", "yes"}:
        session_high_post_entry = _number(metadata.get("session_high_post_entry"))
        if session_high_post_entry > 0:
            peak = max(peak, session_high_post_entry)

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
    # 确定性落袋锁阈值 (2026-09-19): env EXT_CERT_LOCK_PCT 全局开关 (0/未设 ⇒ 关闭,
    #   生产默认行为不变), metadata.cert_lock_pct 可按仓覆盖。
    cert_lock_pct = _number(
        metadata.get("cert_lock_pct", os.getenv("EXT_CERT_LOCK_PCT") or 0.0)
    )
    cert_line = entry * (1 + sign * cert_lock_pct / 100.0)
    cert_lock_reached = bool(
        cert_lock_pct > 0
        and (current >= cert_line if long_position else current <= cert_line)
    )

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
        elif cert_lock_pct > 0 and cert_lock_reached:
            # 2026-09-19 用户令(首要目标: 每笔净收益不亏损; 其次高收益+回撤守住):
            #   确定性落袋锁 — 触及 +k% 即全量落袋, **不走 RR 门**。RR 门是"风险回报比"
            #   口径, 与"确定性落袋"目标正交: 初始 SL -5% 时 +3% 的 RR 仅 0.6 <
            #   min_tp_rr 1.5 ⇒ 走 RR 门必然成为死门(这正是 ext 仓 take_profit 恒 None
            #   之外的第二个结构性拦截)。优先级: 显式 take_profit > 确定性落袋锁 >
            #   亏损侧不动作 (盘后不砍亏, 用户 9/17 令)。
            if profit_pct - fee_cushion_pct <= 0:
                action = "hold"
                detail = "cert_lock_below_net_fee"
            else:
                action = "SL_CERT_LOCK"
                detail = "certainty_lock_line_reached"
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
        "cert_lock_pct": cert_lock_pct if cert_lock_pct > 0 else None,
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
