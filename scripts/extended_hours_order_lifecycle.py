#!/usr/bin/env python3
"""Futu extended-hours order lifecycle helpers shared by entry/exit executors.

RFC-191 (2026-11-09) — 轮询口径收敛:
    Futu `order_list_query` 的「同一账户每 30 秒最多 10 次」**只对 refresh_cache=True 生效**
    (官方文档「调用此接口，只有在刷新缓存时，才受到限频限制」; 只读探针 2026-11-09 21:09 CST
    实测: refresh=True 与 refresh=False 返回**逐行一致**的 12 行, 前者 0.077s / 后者 0.015s,
    且 2.9 秒内连发 12 次 refresh=False 零报错)。
    原实现把 `refresh_cache=True` 写死在**轮询循环**里 (wait_order_fill 每 3s 一次 × 90s
    ⇒ 单笔等成交就发 30 次刷新 ≈ 60 次/分钟), 一个订单就吃光全账户额度, 把 entry/exit 的其它
    查询打成 `查询未完成订单频率太高`。
    本模块因此改为:
      ① 轮询走 OpenD 缓存 (服务端推送同步, 免费), 默认不刷新;
      ② 刷新只保留在「怀疑缓存失真 / 需要服务端真值」的少数时点: 缓存里看不到刚下的单、
         撤单前确认、终态未确认时复核;
      ③ 刷新再过一道进程内节流: 同一 (env, acc_id) 至少 REFRESH_MIN_INTERVAL_S 秒一次,
         除非显式 force_refresh=True (每笔等成交最多 2 次, 仍在 10/30s 额度内);
      ④ 刷新撞限频时自动退回缓存读 (免费且推送同步), 不把一个可观测性读取升级成异常。
"""
import os
import sys
import time

# 同一 (env, acc_id) 两次「刷新」之间的最小间隔。Futu 额度 = 10 次/30s ⇒ 3s 是理论上限,
# 取 6s 留一半余量给 exit 链/看板等其它刷新调用方。
REFRESH_MIN_INTERVAL_S = float(os.getenv("FUTU_ORDER_REFRESH_MIN_INTERVAL_S", "6") or 6)

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

# 限频文案特征 (Futu 简中/英文两种形态)。命中即视为「共享额度已紧」, 走缓存兜底而不是报错。
THROTTLE_MARKERS = ("频率", "最多", "too frequent", "throttl", "rate limit")

_last_refresh_at = {}
refresh_stats = {"allowed": 0, "suppressed": 0, "throttle_fallback": 0}


def _is_throttle_error(data) -> bool:
    text = str(data).lower()
    return any(marker in text for marker in THROTTLE_MARKERS)


def _refresh_allowed(env, acc_id, force=False) -> bool:
    """同一 (env, acc_id) 的刷新节流。force=True 跳过节流但仍记账。"""
    key = (str(env), str(acc_id))
    now = time.monotonic()
    last = _last_refresh_at.get(key)
    if not force and last is not None and (now - last) < REFRESH_MIN_INTERVAL_S:
        refresh_stats["suppressed"] += 1
        return False
    _last_refresh_at[key] = now
    refresh_stats["allowed"] += 1
    return True


def reset_refresh_state():
    """测试用: 清空节流状态与计数 (生产不调用)。"""
    _last_refresh_at.clear()
    for key in refresh_stats:
        refresh_stats[key] = 0


def _order_list_query(ctx, env, acc_id, refresh, **kwargs):
    return ctx.order_list_query(
        refresh_cache=refresh, trd_env=env, acc_id=acc_id, **kwargs
    )


def _query_with_throttle_fallback(ctx, env, acc_id, refresh, ret_ok, **kwargs):
    """刷新模式撞限频 → 退回缓存读 (推送同步, 免费), 保留原返回码语义。"""
    ret, data = _order_list_query(ctx, env, acc_id, refresh, **kwargs)
    if ret == ret_ok or not refresh or not _is_throttle_error(data):
        return ret, data
    ret2, data2 = _order_list_query(ctx, env, acc_id, False, **kwargs)
    if ret2 == ret_ok:
        refresh_stats["throttle_fallback"] += 1
        print(
            f"[ext_orders] 刷新撞限频, 已退回缓存读: {str(data)[:100]}",
            file=sys.stderr,
        )
        return ret2, data2
    return ret, data


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


def query_order_rows(ctx, env, acc_id, ret_ok=0, refresh=True, force_refresh=False):
    """未完成订单表 (含 24h 内已成交/已撤).

    `refresh=True` 表示「允许刷新」, 实际是否刷新由节流决定 (同一账户 6s 一次);
    调用方不需要知道结果 —— 缓存读与刷新读返回的是同一份推送同步数据。
    查询失败仍抛 RuntimeError (调用方沿用各自的 fail-closed 语义)。
    """
    _refresh = bool(refresh) and _refresh_allowed(env, acc_id, force=force_refresh)
    ret, data = _query_with_throttle_fallback(ctx, env, acc_id, _refresh, ret_ok)
    if ret != ret_ok:
        raise RuntimeError(f"order_list_query failed: {data}")
    if data is None or data.empty:
        return []
    return [_order_row(row) for _, row in data.iterrows()]


def query_order(ctx, env, acc_id, order_id, ret_ok=0, refresh=False, force_refresh=False):
    """单笔订单状态。默认走缓存 (轮询路径), refresh=True 时才向服务端刷新。"""
    _refresh = bool(refresh) and _refresh_allowed(env, acc_id, force=force_refresh)
    ret, data = _query_with_throttle_fallback(
        ctx, env, acc_id, _refresh, ret_ok, order_id=order_id
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

    started = time.time()
    deadline = started + timeout_s
    last_order = None
    last_error = ""
    polls = 0
    refreshes = 0
    cache_misses = 0
    while True:
        try:
            order = query_order(ctx, env, acc_id, order_id, ret_ok)
            polls += 1
        except Exception as exc:
            order = None
            last_error = str(exc)
        if order is None and cache_misses < 2:
            cache_misses += 1
        if order is None and cache_misses >= 2 and refreshes == 0:
            # 缓存里连续两轮看不到这笔单 (推送延迟/缓存失真) ⇒ 用一次强制刷新兜底。
            try:
                order = query_order(
                    ctx, env, acc_id, order_id, ret_ok,
                    refresh=True, force_refresh=True,
                )
                refreshes += 1
            except Exception as exc:
                last_error = str(exc)
        if order:
            last_order = order
            status = order["status"]
            if status == "FILLED_ALL" and order["filled_qty"] > 0:
                _log_wait(order_id, polls, refreshes, started, status)
                return _result(order, qty)
            if status in TERMINAL_ORDER_STATUSES:
                _log_wait(order_id, polls, refreshes, started, status)
                return _result(order, qty)
            if status not in ACTIVE_ORDER_STATUSES:
                _log_wait(order_id, polls, refreshes, started, status)
                return _result(order, qty, cancel_ok=False, error="unknown_order_status")
        if time.time() >= deadline:
            break
        time.sleep(poll_s)

    # 撤单前用服务端真值确认一次 (整个 wait 只这一次强制刷新):
    #   否则可能凭陈旧缓存撤掉一笔已成交的单, 或把真成交报成 cancel_not_confirmed。
    try:
        confirmed = query_order(
            ctx, env, acc_id, order_id, ret_ok, refresh=True, force_refresh=True
        )
        refreshes += 1
    except Exception as exc:
        confirmed = None
        last_error = str(exc)
    if confirmed:
        last_order = confirmed
        if confirmed["status"] in TERMINAL_ORDER_STATUSES:
            _log_wait(order_id, polls, refreshes, started, confirmed["status"])
            return _result(confirmed, qty)

    if last_order is None:
        _log_wait(order_id, polls, refreshes, started, "no_order_row")
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
                _log_wait(order_id, polls, refreshes, started, order["status"])
                return _result(order, qty)
        time.sleep(poll_s)

    # 终态仍未确认: 用一次刷新取真值, 让 error 里带的是 broker 真状态而不是缓存猜测。
    try:
        final = query_order(
            ctx, env, acc_id, order_id, ret_ok, refresh=True, force_refresh=True
        )
        refreshes += 1
        if final:
            last_order = final
    except Exception as exc:
        last_error = str(exc)

    _log_wait(order_id, polls, refreshes, started,
              last_order.get("status") if last_order else "unknown")
    return _result(
        last_order,
        qty,
        cancel_ok=False,
        error=f"cancel_not_confirmed:{last_order.get('status') if last_order else last_error}",
    )


def _log_wait(order_id, polls, refreshes, started, status):
    """一次等成交的口径留痕: 轮询次数 / 刷新次数 / 耗时 (验证轮询已去放大)。"""
    print(
        f"[ext_orders] wait_order_fill oid={order_id} polls={polls} "
        f"refreshes={refreshes} elapsed={time.time() - started:.1f}s status={status}"
    )