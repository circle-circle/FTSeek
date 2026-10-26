"""Record-only broker capacity and active-order evidence before REAL orders.

This module deliberately never rejects or rewrites an order.  It is stage one of
P0-2: collect `acctradinginfo_query` capacity, the requested share quantity and
same-symbol active orders, then leave the existing submission path untouched.
All failures are fail-open and only emit a warning.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "broker_capacity_shadow.v1"
_WRITE_LOCK = threading.Lock()
_DEFAULT_PATH = Path(__file__).resolve().parents[2] / "data" / "broker_capacity_shadow.jsonl"
_TERMINAL_STATUSES = frozenset({
    "FILLED_ALL", "CANCELLED_ALL", "CANCELLED_PART", "FAILED", "DISABLED", "DELETED",
})
_CAPACITY_FIELDS = (
    "max_cash_buy", "max_cash_and_margin_buy", "max_position_sell",
    "max_sell_short", "max_buy_back", "long_required_im", "short_required_im",
)


def _token(value: Any) -> str:
    if value is None:
        return ""
    return str(getattr(value, "value", value)).rsplit(".", 1)[-1].upper()


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _json_scalar(value: Any) -> Any:
    """Convert pandas/numpy scalars to JSON-safe values without inventing data."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    number = _number(value)
    if number is not None:
        return int(number) if number.is_integer() else number
    if isinstance(value, str):
        return value
    try:
        return str(value)
    except Exception:
        return None


def capacity_field_for_order(direction: Any, is_exit: bool) -> str:
    """Map position direction + entry/exit to Futu's four capacity fields."""
    side = _token(direction)
    if side in {"LONG", "BUY"}:
        return "max_position_sell" if is_exit else "max_cash_buy"
    if side in {"SHORT", "SELL", "SELL_SHORT"}:
        return "max_buy_back" if is_exit else "max_sell_short"
    return ""


def _request_side(direction: Any, is_exit: bool) -> str:
    side = _token(direction)
    if side in {"LONG", "BUY"}:
        return "SELL" if is_exit else "BUY"
    if side in {"SHORT", "SELL", "SELL_SHORT"}:
        return "BUY" if is_exit else "SELL"
    return ""


def _price_for_order(order: Any) -> float:
    price = _number(getattr(order, "price", 0)) or 0.0
    if price > 0:
        return price
    metadata = getattr(order, "metadata", None)
    if isinstance(metadata, Mapping):
        return _number(metadata.get("reference_price")) or 0.0
    return 0.0


def query_capacity(
    ctx: Any,
    *,
    symbol: str,
    price: float,
    trd_env: Any,
    acc_id: Any,
    lock: Any = None,
    capacity_field: str,
    requested_qty: float | None = None,
) -> dict:
    """Read one broker capacity row.  Query failures return ``unknown``."""
    result = {
        "capacity_field": capacity_field,
        "capacity_qty": None,
        "capacity_status": "unknown",
        "capacity_all": None,
        "capacity_error": "",
    }
    if not capacity_field:
        result["capacity_error"] = "unsupported_direction"
        return result
    if not symbol or not (_number(price) or 0) > 0:
        result["capacity_error"] = "price_unavailable"
        return result
    try:
        from futu import OrderType, RET_OK

        with (lock or nullcontext()):
            ret, data = ctx.acctradinginfo_query(
                order_type=OrderType.NORMAL,
                code=str(symbol),
                price=float(price),
                trd_env=trd_env,
                acc_id=int(acc_id) if acc_id else 0,
            )
        if ret != RET_OK or data is None or getattr(data, "empty", True):
            result["capacity_error"] = f"acctradinginfo_query_failed: {data}"
            return result
        row = data.iloc[0]
        capacity_all = {field: _json_scalar(row.get(field)) for field in _CAPACITY_FIELDS}
        capacity_all["session"] = str(row.get("session", "") or "")
        capacity_qty = _number(row.get(capacity_field))
        result["capacity_all"] = capacity_all
        result["capacity_qty"] = capacity_qty
        if capacity_qty is None:
            result["capacity_error"] = "capacity_field_missing"
            return result
        requested = _number(requested_qty)
        result["capacity_status"] = (
            "unknown" if requested is None
            else "sufficient" if capacity_qty >= requested
            else "insufficient"
        )
        return result
    except Exception as error:
        logger.warning(
            "[capacity-shadow] acctradinginfo_query failed %s: %s",
            symbol, error, exc_info=True,
        )
        result["capacity_error"] = f"{type(error).__name__}: {error}"
        return result


def query_active_orders(
    ctx: Any,
    *,
    symbol: str,
    trd_env: Any,
    acc_id: Any,
    lock: Any = None,
) -> dict:
    """Read same-symbol active orders without treating them as a gate."""
    result = {
        "success": False,
        "orders": [],
        "active_order_count": None,
        "external_active_order_count": None,
        "error": "",
    }
    try:
        from futu import RET_OK

        with (lock or nullcontext()):
            ret, data = ctx.order_list_query(
                code=str(symbol),
                trd_env=trd_env,
                acc_id=int(acc_id) if acc_id else 0,
                refresh_cache=True,
            )
        if ret != RET_OK or data is None:
            result["error"] = f"order_list_query_failed: {data}"
            return result
        rows = [] if getattr(data, "empty", True) else data.to_dict("records")
        active = []
        for row in rows:
            status = _token(row.get("order_status"))
            if status in _TERMINAL_STATUSES:
                continue
            remark = str(row.get("remark", "") or "")
            active.append({
                "order_id": str(row.get("order_id", "") or ""),
                "code": str(row.get("code", "") or ""),
                "trd_side": _token(row.get("trd_side")),
                "order_type": _token(row.get("order_type")),
                "order_status": status,
                "qty": _json_scalar(row.get("qty")),
                "dealt_qty": _json_scalar(row.get("dealt_qty")),
                "price": _json_scalar(row.get("price")),
                "aux_price": _json_scalar(row.get("aux_price")),
                "remark": remark,
                "external": not remark.startswith("pb_"),
            })
        result.update(
            success=True,
            orders=active,
            active_order_count=len(active),
            external_active_order_count=sum(1 for order in active if order["external"]),
        )
        return result
    except Exception as error:
        logger.warning(
            "[capacity-shadow] active-order query failed %s: %s",
            symbol, error, exc_info=True,
        )
        result["error"] = f"{type(error).__name__}: {error}"
        return result


def build_capacity_record(
    order: Any,
    *,
    target_env: str,
    is_exit: bool,
    capacity: Mapping[str, Any] | None = None,
    active: Mapping[str, Any] | None = None,
    now: float | None = None,
) -> dict:
    """Build one stable JSONL row.  Quantities are shares and prices are USD."""
    capacity = dict(capacity or {})
    active = dict(active or {})
    direction = getattr(order, "direction", "")
    requested_qty = _number(getattr(order, "quantity", 0)) or 0.0
    record = {
        "schema_version": SCHEMA_VERSION,
        "recorded_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if now is None else now)
        ),
        "record_only": True,
        "target_env": str(target_env),
        "symbol": str(getattr(order, "symbol", "") or ""),
        "intent": "exit" if is_exit else "entry",
        "position_direction": _token(direction),
        "request_side": _request_side(direction, is_exit),
        "requested_qty": requested_qty,
        "quantity_unit": "shares",
        "price_usd": round(_price_for_order(order), 6),
        "client_order_id": str(getattr(order, "client_order_id", "") or ""),
        "capacity_field": capacity.get("capacity_field", ""),
        "capacity_qty": capacity.get("capacity_qty"),
        "capacity_status": capacity.get("capacity_status", "unknown"),
        "capacity_all": capacity.get("capacity_all"),
        "capacity_error": str(capacity.get("capacity_error", "") or ""),
        "active_orders": list(active.get("orders") or []),
        "active_order_count": active.get("active_order_count"),
        "external_active_order_count": active.get("external_active_order_count"),
        "active_orders_error": str(active.get("error", "") or ""),
    }
    return record


def append_capacity_record(record: Mapping[str, Any], *, path: str | os.PathLike | None = None) -> str:
    """Append one JSONL row and return the target path."""
    target = Path(path or os.getenv("BROKER_CAPACITY_SHADOW_PATH", "") or _DEFAULT_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(dict(record), ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    with _WRITE_LOCK:
        with target.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    return str(target)


def record_order_capacity_shadow(
    ctx: Any,
    *,
    acc_id: Any,
    trd_env: Any,
    lock: Any,
    order: Any,
    target_env: str,
    is_exit: bool,
    path: str | os.PathLike | None = None,
) -> dict | None:
    """Record REAL order capacity evidence.  Never raises into the order path."""
    if target_env != "real":
        return None
    record = None
    try:
        symbol = str(getattr(order, "symbol", "") or "")
        direction = getattr(order, "direction", "")
        requested_qty = _number(getattr(order, "quantity", 0)) or 0.0
        price = _price_for_order(order)
        capacity = query_capacity(
            ctx,
            symbol=symbol,
            price=price,
            trd_env=trd_env,
            acc_id=acc_id,
            lock=lock,
            capacity_field=capacity_field_for_order(direction, is_exit),
            requested_qty=requested_qty,
        )
        active = query_active_orders(
            ctx, symbol=symbol, trd_env=trd_env, acc_id=acc_id, lock=lock,
        )
        record = build_capacity_record(
            order, target_env=target_env, is_exit=is_exit,
            capacity=capacity, active=active,
        )
        append_capacity_record(record, path=path)
    except Exception as error:
        logger.warning(
            "[capacity-shadow] record failed (order path unchanged): %s",
            error, exc_info=True,
        )
    return record
