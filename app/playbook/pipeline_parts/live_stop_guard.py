"""LIVE 券商 STOP 保护族 (reconcile / release / rehang / halt / alert)。

[阶段15 2026-10-28] 逐字迁自 app/playbook/pipeline.py L3141-3630 (490 行, 11 函数 + 4 常量),
零行为变更。迁移理由: pipeline.py 行数棘轮 (test_pipeline_size_ratchet, 基线 3700) 卡在
4136——该文件 +328 的增量正是 10-26 归档 WIP 的这批 LIVE STOP 保护逻辑, 属 DEB-20261008
§9.5 拆分路线"新增逻辑进新模块"的收口 (阶段 15)。

依赖约定:
  - 本族只被 pipeline.py 的出场链/夜清链使用: pipeline 以 kwargs 把可调用对象传给
    exit_monitor_flow / overnight_cleanup, 并对 tests 保 `pipeline.<name>` re-export。
  - `_close_trade_record` 仍走 pipeline 的转发器 (本体在 pipeline_parts.close_flow, 转发器
    注入 datetime/_now_et/logger/_sl_burst_alerted 等缝) ⇒ 函数内 lazy import 防循环依赖,
    与 exit_classification.py / shadow_telemetry.py 同款。
  - 常量 (_LIVE_GUARD_ALERTED / _BROKER_STOP_PARTIAL_ALERTED / _TERMINAL_ORDER_STATUSES /
    _UNSAFE_CLOSE_ERROR_MARKERS) 仅本族使用, 随之迁入; tests 需 patch 时指向本模块。

回滚: 把下方函数体粘回 pipeline.py 原位置并删本文件 (纯搬家, 无状态迁移)。
"""
from __future__ import annotations

import json
import logging
import math
import time
from datetime import date, datetime, timezone
from typing import Any, Dict

logger = logging.getLogger(__name__)


def _reconcile_broker_stop_exit(dual, conn, row, meta) -> bool:
    """Book a live position already closed by its broker-side STOP order."""
    if row.get("source_type") != "PLAYBOOK_LIVE":
        return False
    stop_order_id = str(meta.get("sl_broker_order_id") or "").strip()
    if not stop_order_id:
        return False

    try:
        filled_price, filled_qty = dual.query_order_fill(
            stop_order_id, target_env="real"
        )
    except Exception as error:
        logger.warning(
            "[playbook live] broker STOP fill reconciliation pending "
            f"{row.get('symbol')} stop_id={stop_order_id}: {error}",
            exc_info=True,
        )
        return False

    quantity = float(row.get("quantity") or 0)
    if not filled_price or filled_qty != quantity:
        return False

    entry_price = float(row["entry_price"] or 0)
    direction = str(row["direction"] or "").upper()
    pnl = (
        (filled_price - entry_price) * quantity
        if direction == "LONG"
        else (entry_price - filled_price) * quantity
    )
    # [阶段15] 仍走 pipeline 的转发器 (本体在 close_flow, 转发器注入 datetime/_now_et/logger
    # /_sl_burst_alerted 等缝); lazy import 防 pipeline ⇄ live_stop_guard 循环依赖。
    from app.playbook import pipeline as _pipeline
    booked = _pipeline._close_trade_record(
        conn,
        int(row["id"]),
        float(filled_price),
        "SL_BROKER_STOP",
        pnl,
        pnl / (entry_price * quantity),
        0.0,
        symbol=str(row["symbol"]),
        close_futu_order_id=stop_order_id,
        exit_metadata={
            "exit_price_source": "broker_stop_reconciled",
            "broker_stop_reconciled": True,
            "broker_stop_order_id": stop_order_id,
            "broker_stop_dealt_qty": filled_qty,
        },
        fee_broker=dual,
    )
    if booked:
        logger.info(
            f"[playbook live] broker STOP fill reconciled {row['symbol']} "
            f"qty={quantity} price={filled_price} stop_id={stop_order_id}"
        )
    return bool(booked)


def _release_live_broker_stop_for_rule_exit(dual, conn, row, meta) -> Dict[str, Any]:
    """Release only this arm's broker STOP before a LIVE rule exit.

    The normal broker STOP remains the offline protection. When an online rule
    exit needs to close the position, this gate first books a raced STOP fill,
    otherwise cancels the STOP and waits for a terminal no-fill cancellation.
    It never releases an unmatched/manual order.
    """
    if row.get("source_type") != "PLAYBOOK_LIVE":
        return {"action": "proceed"}
    stop_order_id = str(meta.get("sl_broker_order_id") or "").strip()
    if not stop_order_id:
        return {"action": "proceed"}

    symbol = str(row.get("symbol") or "")
    direction = str(row.get("direction") or "").upper()
    quantity = float(row.get("quantity") or 0)
    expected_side = "SELL" if direction == "LONG" else "BUY"
    expected_remark = f"pb_stop_{symbol}_real"
    terminal_no_fill = {
        "CANCELLED_ALL", "CANCELLED_PART", "FAILED", "DISABLED", "DELETED",
    }

    def _status():
        raw = dual.query_order_status(stop_order_id, target_env="real")
        if not isinstance(raw, dict) or not raw.get("success"):
            error = str(raw.get("error", "broker STOP status unavailable") if isinstance(raw, dict) else raw)
            return None, f"broker_stop_status_pending: {error}"
        return raw, ""

    def _identity_matches(raw):
        try:
            raw_qty = float(raw.get("qty", 0) or 0)
        except (TypeError, ValueError):
            raw_qty = -1.0
        values = {
            "order_id": str(raw.get("order_id", "") or "").strip(),
            "code": str(raw.get("code", "") or "").strip(),
            "trd_side": str(raw.get("trd_side", "") or "").rsplit(".", 1)[-1].upper(),
            "order_type": str(raw.get("order_type", "") or "").rsplit(".", 1)[-1].upper(),
            "remark": str(raw.get("remark", "") or "").strip(),
        }
        return (
            values["order_id"] == stop_order_id
            and values["code"] == symbol
            and values["trd_side"] == expected_side
            and values["order_type"] == "STOP"
            and values["remark"] == expected_remark
            and raw_qty == quantity
        )

    def _dealt_qty(raw):
        try:
            return float(raw.get("dealt_qty", 0) or 0)
        except (TypeError, ValueError):
            return -1.0

    raw, error = _status()
    if raw is None:
        return {"action": "hold", "error": error}
    if not _identity_matches(raw):
        logger.error(
            "[playbook live] broker STOP identity mismatch "
            f"{symbol} stop_id={stop_order_id}"
        )
        return {"action": "hold", "error": "broker_stop_identity_mismatch"}

    dealt_qty = _dealt_qty(raw)
    if dealt_qty > 0:
        if _reconcile_broker_stop_exit(dual, conn, row, meta):
            return {"action": "reconciled"}
        _alert_broker_stop_partial_fill(
            symbol, stop_order_id, dealt_qty, quantity, row.get("direction")
        )
        return {"action": "hold", "error": "broker_stop_fill_reconcile_pending"}
    if str(raw.get("order_status", "") or "").upper() in terminal_no_fill:
        return {"action": "proceed"}

    cancel_result = dual.cancel_order(stop_order_id, target_env="real")
    if not cancel_result.get("success"):
        raw, error = _status()
        if (
            raw is not None
            and _identity_matches(raw)
            and _dealt_qty(raw) == 0
            and str(raw.get("order_status", "") or "").upper() in terminal_no_fill
        ):
            return {"action": "proceed"}
        return {"action": "hold", "error": "broker_stop_cancel_failed"}

    for _ in range(20):
        time.sleep(0.25)
        raw, error = _status()
        if raw is None:
            return {"action": "hold", "error": error}
        if not _identity_matches(raw):
            return {"action": "hold", "error": "broker_stop_identity_mismatch"}
        dealt_qty = _dealt_qty(raw)
        if dealt_qty > 0:
            if _reconcile_broker_stop_exit(dual, conn, row, meta):
                return {"action": "reconciled"}
            _alert_broker_stop_partial_fill(
                symbol, stop_order_id, dealt_qty, quantity, row.get("direction")
            )
            return {"action": "hold", "error": "broker_stop_fill_reconcile_pending"}
        if str(raw.get("order_status", "") or "").upper() in terminal_no_fill:
            logger.info(
                "[playbook live] broker STOP released before rule exit "
                f"{symbol} stop_id={stop_order_id}"
            )
            return {"action": "proceed"}

    return {"action": "hold", "error": "broker_stop_cancel_pending"}


_LIVE_GUARD_ALERTED: set = set()
_BROKER_STOP_PARTIAL_ALERTED: set = set()
_TERMINAL_ORDER_STATUSES = frozenset({
    "FILLED_ALL", "CANCELLED_ALL", "CANCELLED_PART", "FAILED", "DISABLED", "DELETED",
})
_UNSAFE_CLOSE_ERROR_MARKERS = (
    "pending", "identity", "ledger", "account_mismatch", "reconciliation",
    "quantity_unknown", "broker_exception",
)


def _alert_broker_stop_partial_fill(symbol: str, stop_order_id: str, dealt_qty: float,
                                    requested_qty: float, direction: str = "") -> None:
    """Aggregate one alert per STOP order; never auto-close the residual."""
    try:
        key = f"{symbol}:{date.today().isoformat()}:{stop_order_id}"
        if key in _BROKER_STOP_PARTIAL_ALERTED:
            return
        _BROKER_STOP_PARTIAL_ALERTED.add(key)
        if len(_BROKER_STOP_PARTIAL_ALERTED) > 512:
            _BROKER_STOP_PARTIAL_ALERTED.clear()
        logger.error(
            "[playbook live] broker STOP partial/uncertain fill %s %s qty=%s/%s "
            "stop_id=%s — keep HELD, no auto residual close",
            symbol, direction, dealt_qty, requested_qty, stop_order_id,
        )
        from app.push import get_push_hub
        from app.push.message import Level as _PushLevel
        get_push_hub().push_text(
            f"⚠️ [券商保护单部分成交] {symbol} {direction} "
            f"已成交 {dealt_qty:g}/{requested_qty:g} 股, STOP={stop_order_id}. "
            f"系统保持 HELD, 不自动平残余, 请人工核对券商持仓与退出意图.",
            level=_PushLevel.HIGH,
        )
    except Exception as error:
        logger.warning(
            "[playbook live] broker STOP partial-fill alert failed %s: %s", symbol, error,
            exc_info=True,
        )


def _halt_live_stop_protection_gap(symbol: str, reason: str, detail: str,
                                   stop_order_id: str = "") -> dict:
    """HIGH alert + HALT_NEW_LIVE without submitting any replacement sell order."""
    key = f"{symbol}:{date.today().isoformat()}:{reason}"
    try:
        if key not in _LIVE_GUARD_ALERTED:
            _LIVE_GUARD_ALERTED.add(key)
            if len(_LIVE_GUARD_ALERTED) > 512:
                _LIVE_GUARD_ALERTED.clear()
            logger.critical(
                "[playbook live] STOP protection gap %s reason=%s detail=%s stop_id=%s "
                "— no replacement sell submitted; halting new LIVE entries",
                symbol, reason, detail, stop_order_id,
            )
            from app.push import get_push_hub
            from app.push.message import Level as _PushLevel
            get_push_hub().push_text(
                f"🚨 [LIVE 保护缺口] {symbol} {reason}: {detail}. "
                f"系统未重复挂卖单, 已 HALT_NEW_LIVE; 请立即核对券商持仓、活动单与保护 STOP.",
                level=_PushLevel.HIGH,
            )
        from app.playbook.live_arm_gate import halt_live_new_positions
        state = halt_live_new_positions(f"broker_stop_gap:{symbol}:{reason}")
    except Exception as error:
        logger.error(
            "[playbook live] STOP protection-gap HALT failed %s: %s", symbol, error,
            exc_info=True,
        )
        state = {"halted": False, "reason": "halt_write_failed"}
    return {"action": "halted", "reason": reason, "detail": detail, "halt": state}


def _connection_db_path(conn) -> str:
    try:
        row = next(r for r in conn.execute("PRAGMA database_list") if r[1] == "main")
        return str(row[2] or "")
    except Exception:
        return ""


def _persist_live_stop_rehang_state(conn, trade_id: int, updates: Dict[str, Any], *,
                                    stop_id: str = "", symbol: str = "") -> bool:
    """Persist the one-shot guard/receipt before/after the broker call."""
    import sqlite3

    db_path = _connection_db_path(conn)
    if not db_path or db_path == ":memory:":
        return False
    db = sqlite3.connect(db_path, timeout=5)
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT metadata_json FROM trade_records WHERE id=?", (int(trade_id),)
        ).fetchone()
        if row is None:
            raise ValueError("LIVE trade record missing")
        try:
            metadata = json.loads(row[0] or "{}")
        except (TypeError, ValueError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        metadata.update(updates)
        db.execute(
            "UPDATE trade_records SET metadata_json=? WHERE id=?",
            (json.dumps(metadata, ensure_ascii=False), int(trade_id)),
        )
        if stop_id and symbol:
            db.execute(
                "UPDATE open_positions SET sl_broker_order_id=? "
                "WHERE symbol=? AND account_type='live' "
                "AND owner_strategy='playbook_live' AND owner='auto'",
                (str(stop_id), str(symbol)),
            )
        db.commit()
        return True
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _stop_price_from_metadata(meta: Dict[str, Any]) -> float:
    candidates = [meta.get("stop_price")]
    broker_stop = meta.get("broker_stop")
    if isinstance(broker_stop, dict):
        candidates.append(broker_stop.get("stop_price"))
    candidates.append(meta.get("stop_loss"))
    for value in candidates:
        try:
            price = float(value or 0)
        except (TypeError, ValueError):
            continue
        if price > 0:
            return price
    return 0.0


def _position_matches_live_short(row, position) -> bool:
    """Require one exact US position row matching direction and full quantity."""
    symbol = str(row.get("symbol") or "")
    direction = str(row.get("direction") or "").upper()
    expected = float(row.get("quantity") or 0)
    if position.get("code") != symbol or expected <= 0:
        return False
    try:
        signed = float(position.get("qty"))
    except (TypeError, ValueError):
        return False
    if not math.isfinite(signed) or abs(abs(signed) - expected) > max(1e-6, expected * 1e-9):
        return False
    side = str(position.get("position_side") or "").rsplit(".", 1)[-1].upper()
    if side not in {"LONG", "SHORT"}:
        side = "SHORT" if signed < 0 else "LONG"
    return side == direction


def _live_close_failure_is_explicit(error: Any) -> bool:
    code = str(error or "").strip()
    if not code:
        return False
    lowered = code.lower()
    return not any(marker in lowered for marker in _UNSAFE_CLOSE_ERROR_MARKERS)


def _maybe_rehang_live_broker_stop(dual, conn, row, meta, close_res) -> Dict[str, Any]:
    """Attempt exactly one protective STOP rehang after an explicit close failure."""
    symbol = str(row.get("symbol") or "")
    stop_order_id = str(meta.get("sl_broker_order_id") or "").strip()
    if row.get("source_type") != "PLAYBOOK_LIVE" or not stop_order_id:
        return {"action": "not_applicable"}
    if meta.get("stop_rehang_attempted") or int(meta.get("stop_rehang_count") or 0) >= 1:
        # The first attempt may have succeeded and its STOP was later released by
        # the next rule-exit try.  Returning silently here would leave the LIVE
        # position unprotected after a second explicit close failure.  Keep the
        # one-shot no-resubmit rule, but surface the gap and stop new LIVE entries.
        return _halt_live_stop_protection_gap(
            symbol, "stop_rehang_already_attempted",
            f"previous_stop_id={stop_order_id}", stop_order_id,
        )

    error = str(close_res.get("error") or "")
    try:
        dealt_qty = float(close_res.get("dealt_qty") or 0)
    except (TypeError, ValueError):
        dealt_qty = 0.0
    if close_res.get("order_filled") or dealt_qty > 0 or not _live_close_failure_is_explicit(error):
        return _halt_live_stop_protection_gap(
            symbol, "close_outcome_unknown", error or "exit evidence pending", stop_order_id,
        )

    stop_price = _stop_price_from_metadata(meta)
    direction = str(row.get("direction") or "").upper()
    quantity = float(row.get("quantity") or 0)
    if stop_price <= 0:
        return _halt_live_stop_protection_gap(
            symbol, "stop_price_missing", f"stop_id={stop_order_id}", stop_order_id,
        )

    attempt_updates = {
        "stop_rehang_attempted": True,
        "stop_rehang_count": int(meta.get("stop_rehang_count") or 0) + 1,
        "stop_rehang_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stop_rehang_close_error": error,
    }
    meta.update(attempt_updates)
    persist_error = None
    try:
        persisted = _persist_live_stop_rehang_state(
            conn, int(row["id"]), attempt_updates, symbol=symbol,
        )

    except Exception as error:
        persist_error = error
        logger.error(
            "[playbook live] rehang guard persist failed %s: %s", symbol, persist_error,
            exc_info=True,
        )
        persisted = False
    if not persisted:
        return _halt_live_stop_protection_gap(
            symbol, "rehang_guard_persist_failed", str(persist_error or "db unavailable"),
            stop_order_id,
        )

    position_result = dual.query_positions("real", refresh_cache=True)
    if not position_result.get("success"):
        return _halt_live_stop_protection_gap(
            symbol, "position_check_unavailable", position_result.get("error", ""), stop_order_id,
        )
    matches = [p for p in position_result.get("positions", []) if p.get("code") == symbol]
    if len(matches) != 1 or not _position_matches_live_short(row, matches[0]):
        return _halt_live_stop_protection_gap(
            symbol, "position_changed_or_ambiguous",
            f"matches={len(matches)} expected_qty={quantity:g}", stop_order_id,
        )

    orders_result = dual.query_symbol_orders(symbol, target_env="real")
    if not orders_result.get("success"):
        return _halt_live_stop_protection_gap(
            symbol, "active_order_check_unavailable", orders_result.get("error", ""), stop_order_id,
        )
    close_side = "SELL" if direction == "LONG" else "BUY"
    active_close_orders = []
    for order in orders_result.get("orders", []):
        status = str(order.get("order_status") or "").rsplit(".", 1)[-1].upper()
        if status in _TERMINAL_ORDER_STATUSES:
            continue
        side = str(order.get("trd_side") or "").rsplit(".", 1)[-1].upper()
        if side == close_side or side not in {"BUY", "SELL"}:
            active_close_orders.append(str(order.get("order_id") or ""))
    if active_close_orders:
        return _halt_live_stop_protection_gap(
            symbol, "active_close_order_present", ",".join(active_close_orders), stop_order_id,
        )

    replacement = dual.place_protective_stop(
        symbol, quantity, direction, stop_price, target_env="real",
    )
    if not isinstance(replacement, dict):
        replacement = {"success": False, "error": f"invalid_result:{type(replacement).__name__}"}
    new_stop_id = str(replacement.get("futu_order_id") or "").strip()
    if not replacement.get("success") or not new_stop_id:
        detail = str(replacement.get("error") or replacement.get("reason") or replacement)
        return _halt_live_stop_protection_gap(
            symbol, "stop_rehang_failed", detail, stop_order_id,
        )

    receipt = {
        "sl_broker_order_id": new_stop_id,
        "stop_rehang_order_id": new_stop_id,
        "stop_rehang_succeeded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stop_rehang_replaced_order_id": stop_order_id,
        "stop_rehang_stop_price": stop_price,
    }
    meta.update(receipt)
    try:
        receipt_saved = _persist_live_stop_rehang_state(
            conn, int(row["id"]), receipt, stop_id=new_stop_id, symbol=symbol,
        )
    except Exception as receipt_error:
        logger.error(
            "[playbook live] rehang receipt persist failed %s: %s", symbol, receipt_error,
            exc_info=True,
        )
        receipt_saved = False
    if not receipt_saved:
        return _halt_live_stop_protection_gap(
            symbol, "rehang_receipt_persist_failed", f"new_stop_id={new_stop_id}", new_stop_id,
        )
    logger.warning(
        "[playbook live] protective STOP rehung once %s qty=%s price=%s old=%s new=%s",
        symbol, quantity, stop_price, stop_order_id, new_stop_id,
    )
    return {"action": "rehung", "futu_order_id": new_stop_id, "stop_price": stop_price}


def _handle_live_rule_exit_failure(dual, conn, row, meta, close_res) -> Dict[str, Any]:
    """Route a rule-exit failure to safe one-shot recovery or HALT_NEW_LIVE."""
    try:
        if row.get("source_type") != "PLAYBOOK_LIVE":
            return {"action": "not_applicable"}
        if not str(meta.get("sl_broker_order_id") or "").strip():
            return {"action": "not_applicable"}
        if close_res.get("success"):
            return {"action": "not_applicable"}
        return _maybe_rehang_live_broker_stop(dual, conn, row, meta, close_res)
    except Exception as error:
        logger.exception(
            "[playbook live] rehang handler failed %s (fail-close to HALT)",
            row.get("symbol"),
        )
        return _halt_live_stop_protection_gap(
            str(row.get("symbol") or ""), "rehang_handler_error", str(error),
            str(meta.get("sl_broker_order_id") or ""),
        )
