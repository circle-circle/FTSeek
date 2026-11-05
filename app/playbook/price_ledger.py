"""Planned, re-priced and filled price/risk views for Playbook instructions."""
from __future__ import annotations

import json
import math
from typing import Any, Dict, Optional


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _positive(*values: Any) -> Optional[float]:
    for value in values:
        number = _number(value)
        if number is not None and number > 0:
            return number
    return None


def _dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, dict) else {}


def calculate_risk_reward(direction: Any, entry_price: Any, stop_loss: Any,
                          take_profit: Any) -> Optional[float]:
    """Return reward/risk only when the three prices have valid directional geometry."""
    entry = _positive(entry_price)
    stop = _positive(stop_loss)
    target = _positive(take_profit)
    if entry is None or stop is None or target is None:
        return None
    normalized = str(direction or "").upper()
    if normalized in {"BUY", "LONG"}:
        risk = entry - stop
        reward = target - entry
    elif normalized in {"SELL_SHORT", "SHORT"}:
        risk = stop - entry
        reward = entry - target
    else:
        return None
    if risk <= 0 or reward <= 0:
        return None
    return round(reward / risk, 4)


def calculate_risk_amount(entry_price: Any, stop_loss: Any,
                          quantity: Any) -> Optional[float]:
    """Return initial dollar risk at the supplied price and actual share count."""
    entry = _positive(entry_price)
    stop = _positive(stop_loss)
    qty = _positive(quantity)
    if entry is None or stop is None or qty is None:
        return None
    return round(abs(entry - stop) * qty, 2)


def _geometry_direction(entry_price: Any, stop_loss: Any,
                        take_profit: Any) -> Optional[str]:
    entry = _positive(entry_price)
    stop = _positive(stop_loss)
    target = _positive(take_profit)
    if entry is None or stop is None or target is None:
        return None
    if stop < entry < target:
        return "BUY"
    if target < entry < stop:
        return "SELL_SHORT"
    return None


def build_price_ledger(instruction: Dict[str, Any]) -> Dict[str, Any]:
    """Build a backward-compatible ledger without mutating the instruction row."""
    raw = _dict(instruction.get("raw_instruction_json"))
    metadata = _dict(instruction.get("metadata_json"))
    reprice = _dict(metadata.get("reprice_audit"))
    entry_fill = _dict(metadata.get("entry_fill"))
    execution_risk = _dict(metadata.get("execution_risk"))
    direction = instruction.get("direction")
    if str(direction or "").upper() not in {"BUY", "LONG", "SELL_SHORT", "SHORT"}:
        direction = raw.get("direction") or direction

    planned_entry = _positive(
        raw.get("planned_entry"), reprice.get("planned_entry"),
        reprice.get("reprice_from"), instruction.get("entry_price"),
    )
    planned_stop = _positive(
        raw.get("planned_stop_loss"), reprice.get("planned_stop_loss"),
        reprice.get("reprice_original_sl"), instruction.get("stop_loss"),
    )
    planned_target = _positive(
        raw.get("planned_take_profit"), reprice.get("planned_take_profit"),
        reprice.get("reprice_original_tp"), instruction.get("take_profit"),
    )
    effective_entry = _positive(instruction.get("entry_price"))
    effective_stop = _positive(instruction.get("stop_loss"))
    effective_target = _positive(instruction.get("take_profit"))
    if str(direction or "").upper() not in {"BUY", "LONG", "SELL_SHORT", "SHORT"}:
        direction = (
            _geometry_direction(planned_entry, planned_stop, planned_target)
            or _geometry_direction(effective_entry, effective_stop, effective_target)
            or direction
        )

    planned_rr = _number(reprice.get("planned_risk_reward"))
    if planned_rr is None and reprice:
        planned_rr = _number(instruction.get("risk_reward"))
    if planned_rr is None:
        planned_rr = calculate_risk_reward(
            direction, planned_entry, planned_stop, planned_target)
    if planned_rr is None and not reprice:
        planned_rr = _number(instruction.get("risk_reward"))

    effective_rr = calculate_risk_reward(
        direction, effective_entry, effective_stop, effective_target)
    fill_price = _positive(
        execution_risk.get("fill_price"), instruction.get("execution_price"),
        entry_fill.get("price"), instruction.get("trade_fill_price"),
    )
    fill_quantity = _positive(
        execution_risk.get("fill_quantity"), entry_fill.get("dealt_qty"),
        entry_fill.get("observed_qty"), instruction.get("trade_fill_quantity"),
    )
    fill_rr = calculate_risk_reward(
        direction, fill_price, effective_stop, effective_target)
    fill_risk_amount = calculate_risk_amount(fill_price, effective_stop, fill_quantity)
    effective_position_size = _positive(instruction.get("position_size"))
    effective_risk_amount = calculate_risk_amount(
        effective_entry, effective_stop, effective_position_size)

    return {
        "planned": {
            "entry_price": planned_entry,
            "stop_loss": planned_stop,
            "take_profit": planned_target,
            "risk_reward": planned_rr,
        },
        "effective": {
            "entry_price": effective_entry,
            "stop_loss": effective_stop,
            "take_profit": effective_target,
            "position_size": effective_position_size,
            "risk_reward": effective_rr,
            "risk_amount": effective_risk_amount,
        },
        "fill": {
            "entry_price": fill_price,
            "stop_loss": effective_stop if fill_price else None,
            "take_profit": effective_target if fill_price else None,
            "quantity": fill_quantity,
            "risk_reward": fill_rr,
            "risk_amount": fill_risk_amount,
        },
        "planned_risk_reward": planned_rr,
        "effective_risk_reward": effective_rr,
        "fill_risk_reward": fill_rr,
        "risk_amount_at_fill": fill_risk_amount,
    }
