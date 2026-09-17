#!/usr/bin/env python3
"""Futu extended-hours order lifecycle helpers shared by entry/exit executors."""
import time


ACTIVE_ORDER_STATUSES = {
    "SUBMITTED",
    "SUBMITTING",
    "UNSUBMITTED",
    "WAITING_SUBMIT",
    "FILLED_PART",
    "CANCELLING_ALL",
    "CANCELLING_PART",
}
TERMINAL_ORDER_STATUSES = {
    "FILLED_ALL",
    "CANCELLED_ALL",
    "CANCELLED_PART",
    "FILL_CANCELLED",
    "DELETED",
    "DISABLED",
    "FAILED",
    "SUBMIT_FAILED",
    "TIMEOUT",
}


def _number(value, default=0.0):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return default


def _order_row(row):
    return {
        "order_id": str(row.get("order_id", "")),
        "symbol": str(row.get("code", "")),
        "side": str(row.get("trd_side", "")),
        "status": str(row.get("order_status", "")),
        "qty": _number(row.get("qty")),
        "filled_qty": _number(row.get("dealt_qty")),
        "avg_fill_price": _number(row.get("dealt_avg_price")),
        "price": _number(row.get("price")),
        "order_type": str(row.get("order_type", "")),
        "session": str(row.get("session", "")),
        "remark": str(row.get("remark", "")),
        "create_time": str(row.get("create_time", "")),
        "update_time": str(row.get("update_time", "")),
    }


def query_order_rows(ctx, env, acc_id, ret_ok=0):
    ret, data = ctx.order_list_query(
        refresh_cache=True, trd_env=env, acc_id=acc_id
    )
    if ret != ret_ok:
        raise RuntimeError(f"order_list_query failed: {data}")
    if data is None or data.empty:
        return []
    return [_order_row(row) for _, row in data.iterrows()]


def query_order(ctx, env, acc_id, order_id, ret_ok=0):
    ret, data = ctx.order_list_query(
        order_id=order_id, refresh_cache=True, trd_env=env, acc_id=acc_id
    )
    if ret != ret_ok:
        raise RuntimeError(f"order query failed for {order_id}: {data}")
    if data is None or data.empty:
        return None
    return _order_row(data.iloc[0])


def cancel_order(ctx, env, acc_id, order_id, qty, price, ret_ok=0):
    from futu import ModifyOrderOp

    ret, data = ctx.modify_order(
        ModifyOrderOp.CANCEL,
        order_id=order_id,
        qty=qty,
        price=price,
        trd_env=env,
        acc_id=acc_id,
    )
    return ret == ret_ok, data


def _result(order, qty, cancel_ok=True, error=""):
    filled_qty = order.get("filled_qty", 0.0) if order else 0.0
    avg_fill_price = order.get("avg_fill_price", 0.0) if order else 0.0
    requested_qty = _number(qty, filled_qty)
    remaining_qty = max(0.0, requested_qty - filled_qty)
    if filled_qty > 0 and avg_fill_price <= 0:
        cancel_ok = False
        error = error or "filled_qty_without_avg_fill_price"
    return {
        "filled": filled_qty > 0 and avg_fill_price > 0,
        "filled_qty": filled_qty,
        "avg_fill_price": avg_fill_price,
        "status": (order or {}).get("status", error or "unknown"),
        "remaining_qty": remaining_qty,
        "cancel_ok": cancel_ok,
        "order": order,
        "error": error,
    }


def wait_order_fill(
    ctx,
    env,
    acc_id,
    order_id,
    qty,
    price,
    timeout_s=90,
    poll_s=3,
    cancel_timeout_s=20,
    ret_ok=0,
):
    if not order_id:
        return _result(None, qty, cancel_ok=False, error="missing_order_id")

    deadline = time.time() + timeout_s
    last_order = None
    last_error = ""
    while True:
        try:
            order = query_order(ctx, env, acc_id, order_id, ret_ok)
        except Exception as exc:
            last_order = None
            last_error = str(exc)
            time.sleep(poll_s)
            continue
        if order:
            last_order = order
            status = order["status"]
            if status == "FILLED_ALL" and order["filled_qty"] > 0:
                return _result(order, qty)
            if status in TERMINAL_ORDER_STATUSES:
                return _result(order, qty)
            if status not in ACTIVE_ORDER_STATUSES:
                return _result(order, qty, cancel_ok=False, error="unknown_order_status")
        if time.time() >= deadline:
            break
        time.sleep(poll_s)

    if last_order is None:
        return _result(None, qty, cancel_ok=False, error=last_error or "order_query_timeout")

    cancel_ok, cancel_data = cancel_order(
        ctx, env, acc_id, order_id, qty, price, ret_ok
    )
    if not cancel_ok:
        return _result(
            last_order,
            qty,
            cancel_ok=False,
            error=f"cancel_request_failed:{cancel_data}",
        )

    cancel_deadline = time.time() + cancel_timeout_s
    while time.time() < cancel_deadline:
        try:
            order = query_order(ctx, env, acc_id, order_id, ret_ok)
        except Exception as exc:
            last_error = str(exc)
            time.sleep(poll_s)
            continue
        if order:
            last_order = order
            if order["status"] in TERMINAL_ORDER_STATUSES:
                return _result(order, qty)
        time.sleep(poll_s)

    return _result(
        last_order,
        qty,
        cancel_ok=False,
        error=f"cancel_not_confirmed:{last_order.get('status') if last_order else last_error}",
    )
