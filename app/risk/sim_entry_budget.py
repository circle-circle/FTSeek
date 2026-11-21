"""Serialize SIM admission reserves; gaps/borrow can exceed this finite budget."""
from __future__ import annotations

import contextvars
import fcntl
import json
import math
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from app.core import paths as core_paths
from app.execution.fee_model import estimate_fee, estimate_round_trip

_THREAD_LOCK = threading.Lock()
_CURRENT_GUARD = contextvars.ContextVar("sim_entry_account_guard", default=None)
_TERMINAL = frozenset({"FILLED_ALL", "CANCELLED_ALL", "CANCELLED_PART", "FAILED", "DISABLED", "DELETED"})
_ZERO_TERMINAL = frozenset({"CANCELLED_ALL", "FAILED", "DISABLED", "DELETED", "DEFINITIVE_REJECT"})


class BudgetUnavailable(ValueError):
    """Unknown truth must defer admission, never poison an instruction."""


def _number(value: Any, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise BudgetUnavailable(label)
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise BudgetUnavailable(label) from error
    if not math.isfinite(number) or (positive and number <= 0):
        raise BudgetUnavailable(label)
    return number


def defer(reason: str) -> dict:
    return {"success": False, "ok": False, "deferred": True,
            "sim_entry_budget_deferred": True, "submission_not_sent": True, "reason": reason,
            "message": f"[SIM_ENTRY_BUDGET] {reason}; retry after reconciliation"}


class SimAccountGuard:
    def __init__(self):
        self.acquired = False
        self.reason = "account_busy"
        self.fd = None
        self.marker_path = None
        self.submission_identity = None

    def _sync_directory(self):
        fd = os.open(str(self.marker_path.parent), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def marker(self):
        if not self.acquired:
            raise BudgetUnavailable("account_lock_not_held")
        try:
            with self.marker_path.open(encoding="utf-8") as stream:
                marker = json.load(stream)
        except FileNotFoundError:
            return None
        if not isinstance(marker, dict) or marker.get("env") != "sim":
            raise BudgetUnavailable("invalid_submission_marker")
        return marker

    def write_marker(self, marker):
        if not self.acquired:
            raise BudgetUnavailable("account_lock_not_held")
        temporary = self.marker_path.with_name(self.marker_path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump(marker, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.marker_path)
            self._sync_directory()
        finally:
            temporary.unlink(missing_ok=True)

    def clear_marker(self):
        if not self.acquired:
            raise BudgetUnavailable("account_lock_not_held")
        self.marker_path.unlink(missing_ok=True)
        self._sync_directory()


@contextmanager
def sim_entry_account_guard(target_env="sim"):
    """Nonblocking thread + process lock, held through SDK send and final booking."""
    if str(target_env).lower() != "sim":
        yield None
        return
    guard = SimAccountGuard()
    owned = _THREAD_LOCK.acquire(blocking=False)
    token = None
    try:
        if owned:
            try:
                guard.marker_path = core_paths.get_data_dir().resolve() / "locks" / "sim-entry-account.json"
                guard.marker_path.parent.mkdir(parents=True, exist_ok=True)
                guard.fd = os.open(str(guard.marker_path.with_suffix(".lock")), os.O_CREAT | os.O_RDWR, 0o600)
                fcntl.flock(guard.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                guard.acquired = True
                token = _CURRENT_GUARD.set(guard)
            except BlockingIOError:
                guard.reason = "account_busy"
            except Exception:
                guard.reason = "account_lock_unavailable"
        yield guard
    finally:
        if token is not None:
            _CURRENT_GUARD.reset(token)
        if guard.fd is not None:
            if guard.acquired:
                fcntl.flock(guard.fd, fcntl.LOCK_UN)
            os.close(guard.fd)
        if owned:
            _THREAD_LOCK.release()


def _guard():
    guard = _CURRENT_GUARD.get()
    if guard is None or not guard.acquired:
        raise BudgetUnavailable("account_lock_not_held")
    return guard


def read_sim_budget_snapshot(symbols, expected_account_id=None, marker_receipt=None):
    """Bound lazy SIM SDK reads without permitting worker-side writes."""
    from app.execution.dual_broker import get_dual_broker
    from futu import OpenQuoteContext, RET_OK

    read_started_at = time.time()
    dual = get_dual_broker()
    ctx, account_id, env, lock = dual._ctx_and_acc("sim")
    if not account_id or str(account_id) != str(expected_account_id or account_id):
        raise BudgetUnavailable("sim_account_identity_mismatch")
    account = dual.get_account_info(target_env="sim")
    positions = dual.query_positions(target_env="sim", refresh_cache=True)
    with lock:
        ret, data = ctx.order_list_query(trd_env=env, acc_id=int(account_id), refresh_cache=True)
    if ret != RET_OK or data is None:
        raise BudgetUnavailable("broker_orders_unavailable")
    orders = data.to_dict("records")
    if marker_receipt:
        if marker_receipt.get("env") != "sim" or str(marker_receipt.get("account_id")) != str(account_id):
            raise BudgetUnavailable("submission_marker_account_mismatch")
        oid = str(marker_receipt.get("order_id") or "")
        client = marker_receipt.get("client_order_id")
        def matches(row):
            return str(row.get("order_id") or "") == oid if oid else bool(client and row.get("remark") == client)
        if (oid or client) and not any(matches(row) for row in orders):
            from zoneinfo import ZoneInfo
            created = datetime.fromisoformat(str(marker_receipt["created_at"]).replace("Z", "+00:00"))
            if created.tzinfo is None:
                raise BudgetUnavailable("submission_marker_timestamp_unknown")
            day = created.astimezone(ZoneInfo("America/New_York")).date().isoformat()
            with lock:
                ret, history = ctx.history_order_list_query(
                    code=marker_receipt["symbol"], start=day, end=day,
                    trd_env=env, acc_id=int(account_id))
            if ret != RET_OK or history is None:
                raise BudgetUnavailable("submission_history_unavailable")
            orders.extend(row for row in history.to_dict("records") if matches(row))
    qctx = OpenQuoteContext(host=os.getenv("FUTU_OPEND_HOST", "127.0.0.1"),
                            port=int(os.getenv("FUTU_OPEND_PORT", "11111")),
                            is_async_connect=True)
    try:
        qctx.set_sync_query_connect_timeout(3.0)
        ret, data = qctx.get_global_state()
        if ret != RET_OK:
            raise BudgetUnavailable("quote_context_not_ready")
        ret, quotes = qctx.get_market_snapshot(list(symbols))
        if ret != RET_OK or quotes is None:
            raise BudgetUnavailable("quotes_unavailable")
        quote_map = {}
        for row in quotes.to_dict("records"):
            quote_map[str(row.get("code") or "")] = {
                "bid": row.get("bid_price"), "ask": row.get("ask_price"),
                # Futu US snapshots report exchange update_time in ET wall time.
                "source_asof": row.get("update_time"), "source": "futu_snapshot",
            }
    finally:
        qctx.close()
    return {"account": account, "account_id": str(account_id),
            "positions": positions, "orders": orders, "quotes": quote_map,
            "read_at": read_started_at}


def _read_local_truth(db_path=None):
    path = Path(db_path or core_paths.get_trade_records_db_for_arm("sim")).resolve()
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")
        peak_rows = conn.execute("SELECT peak_equity FROM capital_state WHERE trade_env=1").fetchall()
        if len(peak_rows) != 1:
            raise BudgetUnavailable("stored_sim_peak_unavailable")
        trades = [dict(row) for row in conn.execute(
            "SELECT * FROM trade_records WHERE exit_time IS NULL OR exit_time=''")]
        positions = [dict(row) for row in conn.execute("SELECT * FROM open_positions WHERE size<>0")]
        booked = [dict(row) for row in conn.execute(
            "SELECT symbol,direction,quantity,source_type,futu_order_id FROM trade_records "
            "WHERE COALESCE(futu_order_id,'')<>''")]
        instructions = [dict(row) for row in conn.execute(
            "SELECT id,execution_state,broker_env,broker_order_id FROM playbook_instructions")]
        unresolved = [dict(row) for row in conn.execute(
            "SELECT * FROM playbook_instructions WHERE execution_state IN "
            "('UNKNOWN','SUBMITTED','FILLED_UNBOOKED','LEGACY_LOCKED') "
            "AND COALESCE(broker_env,'') NOT IN ('real','live')")]
        from app.utils.timezone import now_et
        row = conn.execute(
            "SELECT COALESCE(SUM(COALESCE(pnl_net,realized_pnl)),0) FROM trade_records "
            "WHERE substr(exit_time,1,10)=? AND exit_price>0", (now_et().date().isoformat(),)).fetchone()
        return {"peak": _number(peak_rows[0][0], "stored_sim_peak_invalid", positive=True),
                "trades": trades, "positions": positions, "unresolved": unresolved,
                "booked": booked, "instructions": instructions, "daily_pnl": _number(row[0], "daily_pnl_invalid")}
    finally:
        conn.close()


def _side(value):
    text = str(getattr(value, "value", value)).upper().rsplit(".", 1)[-1]
    if text in {"LONG", "BUY", "BUY_BACK"}:
        return "LONG"
    if text in {"SHORT", "SELL_SHORT", "SELL"}:
        return "SHORT"
    raise BudgetUnavailable("direction_unknown")


def _status(row):
    return str(row.get("order_status") or "").rsplit(".", 1)[-1]


def _quote(quote, max_age, now):
    if not isinstance(quote, dict):
        raise BudgetUnavailable("quote_missing")
    bid = _number(quote.get("bid"), "bid_invalid", positive=True)
    ask = _number(quote.get("ask"), "ask_invalid", positive=True)
    if bid > ask or quote.get("book_is_proxy"):
        raise BudgetUnavailable("quote_book_unknown")
    source = str(quote.get("data_source") or quote.get("source") or "").lower()
    if source in {"finnhub", "finnhub_fallback", "twelvedata", "alphavantage"}:
        raise BudgetUnavailable("quote_source_delayed")
    asof = quote.get("source_asof")
    if asof is not None:
        try:
            stamp = datetime.fromisoformat(str(asof).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                from zoneinfo import ZoneInfo
                stamp = stamp.replace(tzinfo=ZoneInfo("America/New_York"))
            timestamp = stamp.timestamp()
        except (TypeError, ValueError, OverflowError) as error:
            raise BudgetUnavailable("quote_timestamp_invalid") from error
    else:
        timestamp = _number(quote.get("timestamp"), "quote_timestamp_missing", positive=True)
    age = now - timestamp
    if not math.isfinite(age) or age < 0 or age > max_age:
        raise BudgetUnavailable("quote_stale_or_future")
    return bid, ask


def _portfolio(local, broker_positions):
    if not isinstance(broker_positions, dict) or broker_positions.get("success") is not True:
        raise BudgetUnavailable("broker_positions_unavailable")
    broker = {}
    for row in broker_positions.get("positions", []):
        symbol = str(row.get("code") or "")
        qty = _number(row.get("qty"), "broker_quantity_invalid")
        if not qty:
            continue
        if not symbol.startswith("US.") or symbol in broker:
            raise BudgetUnavailable("broker_position_identity_unknown")
        if row.get("position_side") is not None:
            side = _side(row["position_side"])
            if qty > 0 and side == "SHORT":
                qty = -qty
            elif qty < 0 and side != "SHORT":
                raise BudgetUnavailable("broker_position_side_conflict")
        broker[symbol] = qty
    trades = {}
    for row in local["trades"]:
        symbol = str(row.get("symbol") or "")
        if row.get("source_type") != "PLAYBOOK_SIM" or symbol in trades or not symbol.startswith("US."):
            raise BudgetUnavailable("unowned_or_duplicate_open_trade")
        qty = _number(row.get("quantity"), "trade_quantity_invalid", positive=True)
        direction = _side(row.get("direction"))
        trades[symbol] = (qty if direction == "LONG" else -qty, row)
    positions = {}
    for row in local["positions"]:
        account_type = str(row.get("account_type") or "").lower()
        if account_type in {"live", "real"}:
            continue
        if account_type not in {"sim", "simulate", "simulated"}:
            raise BudgetUnavailable("local_position_account_unknown")
        symbol = str(row.get("symbol") or "")
        if symbol in positions:
            raise BudgetUnavailable("duplicate_local_position")
        qty = _number(row.get("size"), "local_quantity_invalid", positive=True)
        direction = _side(row.get("direction"))
        positions[symbol] = qty if direction == "LONG" else -qty
    trade_qty = {symbol: pair[0] for symbol, pair in trades.items()}
    if broker != positions or broker != trade_qty:
        raise BudgetUnavailable("broker_local_portfolio_mismatch")
    return [pair[1] for pair in trades.values()]


def stop_envelope(entry, initial_stop, direction, cooldown_pct):
    entry = _number(entry, "entry_invalid", positive=True)
    stop = _number(initial_stop, "initial_stop_missing", positive=True)
    direction = _side(direction)
    cooldown = _number(cooldown_pct, "cooldown_invalid") / 100.0
    if not 0 <= cooldown < 1:
        raise BudgetUnavailable("cooldown_invalid")
    return min(stop, entry * (1 - cooldown)) if direction == "LONG" else max(stop, entry * (1 + cooldown))


def evaluate_budget(*, equity, peak, threshold, trades, candidate, quotes,
                    cooldown_pct=4.0, cushion_pct=0.2, max_quote_age=60.0, now=None):
    """USD reserves; include LONG/SHORT giveback, fees and configured cushion."""
    equity = _number(equity, "equity_invalid", positive=True)
    peak = max(_number(peak, "peak_invalid", positive=True), equity)
    threshold = _number(threshold, "threshold_invalid", positive=True)
    if threshold >= 1:
        raise BudgetUnavailable("threshold_invalid")
    cushion = _number(cushion_pct, "cushion_invalid") / 100.0
    if cushion < 0:
        raise BudgetUnavailable("cushion_invalid")
    max_age = _number(max_quote_age, "quote_age_invalid", positive=True)
    now = time.time() if now is None else _number(now, "time_invalid", positive=True)
    open_risk = 0.0
    candidate_risk = None
    for item, is_candidate in [(row, False) for row in trades] + [(candidate, True)]:
        symbol = item["symbol"]
        qty = _number(item.get("quantity"), "quantity_invalid", positive=True)
        if qty != int(qty):
            raise BudgetUnavailable("fractional_quantity_unknown")
        direction = _side(item.get("direction"))
        entry = _number(item.get("entry_price"), "entry_invalid", positive=True)
        metadata = item.get("metadata_json") or "{}"
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        stop = stop_envelope(entry, metadata.get("stop_loss"), direction, cooldown_pct)
        bid, ask = _quote(quotes.get(symbol), max_age, now)
        # Equity may be marked above executable bid/below ask: reserve the entire
        # conservative book side to stop, never mid or offsetting other positions.
        current = ask if direction == "LONG" else bid
        if is_candidate:
            current = max(current, entry) if direction == "LONG" else min(current, entry)
        risk = qty * max(current - stop if direction == "LONG" else stop - current, 0.0)
        risk += qty * current * cushion
        if is_candidate:
            risk += estimate_round_trip(qty, current, stop, direction)["total"]
            candidate_risk = risk
        else:
            risk += estimate_fee(qty, stop, "SELL" if direction == "LONG" else "BUY")["total"]
            open_risk += risk
    remaining = equity - (1.0 - threshold) * peak
    total = open_risk + candidate_risk
    return {"allowed": remaining > 0 and total <= remaining,
            "remaining_budget_usd": remaining, "open_risk_usd": open_risk,
            "candidate_risk_usd": candidate_risk, "required_risk_usd": total,
            "peak_equity": peak, "equity": equity}


def _receipt_matches(marker, row):
    qty = _number(row.get("qty"), "receipt_quantity_invalid", positive=True)
    signed = qty if _side(row.get("trd_side")) == "LONG" else -qty
    return (str(row.get("code") or "") == marker["symbol"]
            and signed == marker["signed_qty"]
            and str(row.get("order_id") or "") == marker.get("order_id"))


def _reconcile_marker(guard, snapshot, local):
    marker = guard.marker()
    if marker is None:
        return
    if marker.get("account_id") != snapshot["account_id"]:
        raise BudgetUnavailable("submission_marker_account_mismatch")
    oid = marker.get("order_id")
    if not oid:
        # A client remark may prove identity for Playbook, but EXT's generic remark
        # cannot. Missing evidence is NOT rejection, regardless of elapsed time.
        client = marker.get("client_order_id")
        matches = [row for row in snapshot["orders"] if client and row.get("remark") == client]
        if len(matches) != 1:
            raise BudgetUnavailable("submission_identity_unknown_manual_recovery")
        marker["order_id"] = str(matches[0].get("order_id") or "")
        guard.write_marker(marker)
        oid = marker["order_id"]
    rows = [row for row in snapshot["orders"] if str(row.get("order_id") or "") == oid]
    if len(rows) != 1 or not _receipt_matches(marker, rows[0]):
        raise BudgetUnavailable("submission_receipt_unknown")
    receipt = rows[0]
    status = _status(receipt)
    dealt = _number(receipt.get("dealt_qty"), "receipt_dealt_unknown")
    if dealt < 0 or dealt > abs(marker["signed_qty"]):
        raise BudgetUnavailable("receipt_dealt_conflict")
    if status in _ZERO_TERMINAL and dealt == 0:
        guard.clear_marker()
        return
    if status not in _TERMINAL or dealt <= 0:
        raise BudgetUnavailable("submission_nonterminal")
    booked = [row for row in local["booked"] if str(row.get("futu_order_id") or "") == oid]
    if len(booked) != 1:
        raise BudgetUnavailable("filled_unbooked_submission")
    row = booked[0]
    signed = _number(row.get("quantity"), "booked_quantity_invalid", positive=True)
    signed *= 1 if _side(row.get("direction")) == "LONG" else -1
    expected = dealt * (1 if marker["signed_qty"] > 0 else -1)
    if (row.get("symbol") != marker["symbol"] or signed != expected
            or row.get("source_type") != "PLAYBOOK_SIM"):
        raise BudgetUnavailable("booked_receipt_conflict")
    if marker.get("instruction_id") is not None:
        # Closed positions still need the original BOOKED CAS proof.
        instructions = [item for item in local["instructions"]
                        if str(item["id"]) == str(marker["instruction_id"])]
        if (len(instructions) != 1 or instructions[0]["execution_state"] != "BOOKED"
                or str(instructions[0].get("broker_order_id") or "") != oid
                or str(instructions[0].get("broker_env") or "").lower() != "sim"):
            raise BudgetUnavailable("instruction_booking_unresolved")
    guard.clear_marker()


def _local_risk_identity(local):
    """Ignore heartbeat/trailing observations while detecting changed reserve truth."""
    trades = []
    for row in local["trades"]:
        metadata = row.get("metadata_json") or "{}"
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        trades.append(tuple(row.get(key) for key in (
            "id", "symbol", "quantity", "direction", "entry_price", "source_type", "futu_order_id"
        )) + (metadata.get("stop_loss"),))
    positions = [tuple(row.get(key) for key in ("symbol", "size", "direction", "account_type"))
                 for row in local["positions"]]
    unresolved = [tuple(row.get(key) for key in (
        "id", "symbol", "execution_state", "broker_env", "broker_order_id"))
                  for row in local["unresolved"]]
    return tuple(tuple(sorted(rows, key=str)) for rows in (trades, positions, unresolved))


def _snapshot_equity(snapshot, expected_account_id=None):
    account = snapshot["account"]
    if (account.get("_target_env") != "sim" or account.get("currency") != "USD"
            or not snapshot.get("account_id")
            or (expected_account_id and str(expected_account_id) != str(snapshot["account_id"]))):
        raise BudgetUnavailable("sim_account_identity_unknown")
    read_age = time.time() - _number(snapshot.get("read_at"), "account_timestamp_missing", positive=True)
    if not 0 <= read_age <= 3.0:
        raise BudgetUnavailable("account_snapshot_stale")
    return _number(account.get("total_assets"), "sim_equity_invalid", positive=True)


def sim_entry_preflight(owner, *, source, symbol, quantity, entry_price, stop_loss,
                        direction, expected_account_id=None, db_path=None,
                        reduce_applied=False, apply_daily_reduce=False):
    """Run before claims/send, using final quantities, strict fresh account truth."""
    try:
        guard = _guard()
        local = _read_local_truth(db_path)
        symbols = sorted({symbol} | {row["symbol"] for row in local["trades"]})
        from app.playbook.entry_data_fetch import fetch_entry_sim_budget_snapshot
        snapshot = fetch_entry_sim_budget_snapshot(
            owner, symbols, expected_account_id=expected_account_id, marker_receipt=guard.marker())
        # Exits/recovery do not acquire this entry lock; reread local evidence after
        # the bounded network read and reject any newly unmatched position/stop.
        after_read = _read_local_truth(db_path)
        if _local_risk_identity(local) != _local_risk_identity(after_read):
            raise BudgetUnavailable("local_portfolio_changed_during_read")
        local = after_read
        equity = _snapshot_equity(snapshot, expected_account_id)
        trades = _portfolio(local, snapshot["positions"])
        _reconcile_marker(guard, snapshot, local)
        if local["unresolved"]:
            raise BudgetUnavailable("account_unresolved_entry")
        if any(_status(row) not in _TERMINAL for row in snapshot["orders"]):
            raise BudgetUnavailable("account_nonterminal_broker_order")
        from app.risk.account_circuit_breaker import get_account_circuit_breaker, get_peak_drawdown_threshold
        breaker = get_account_circuit_breaker()
        # Same AccountCB daily thresholds and realized-PNL query contract, with the
        # fresh normalized equity denominator and strict read-only local evidence.
        daily_pnl = local["daily_pnl"]
        if (daily_pnl <= -equity * breaker.liquidate_threshold
                or daily_pnl <= -equity * breaker.halt_threshold):
            raise BudgetUnavailable("account_daily_halt")
        quantity = _number(quantity, "candidate_quantity_invalid", positive=True)
        if quantity != int(quantity):
            raise BudgetUnavailable("candidate_quantity_invalid")
        if daily_pnl <= -equity * breaker.reduce_threshold:
            if apply_daily_reduce:
                quantity = int(quantity * 0.5)
                if quantity <= 0:
                    raise BudgetUnavailable("daily_reduce_below_one_share")
            elif not reduce_applied:
                raise BudgetUnavailable("daily_reduce_changed_retry")
        from app.playbook.config import get_playbook_config
        config = get_playbook_config()
        verdict = evaluate_budget(
            equity=equity, peak=local["peak"], threshold=get_peak_drawdown_threshold("sim"),
            trades=trades, candidate={"symbol": symbol, "quantity": quantity,
                "entry_price": entry_price, "direction": direction,
                "metadata_json": {"stop_loss": stop_loss}}, quotes=snapshot["quotes"],
            cooldown_pct=config.get("cooldown_sl_pct", 4.0),
            cushion_pct=config.get("breakeven_slip_cushion_pct", 0.2),
            max_quote_age=config.get("pretrade_max_quote_age_sec", 60.0))
        if not verdict["allowed"]:
            return {**defer("remaining_budget_insufficient"), **verdict}
        return {"allowed": True, "quantity": int(quantity), "source": source,
                "symbol": symbol, "signed_qty": int(quantity) * (1 if _side(direction) == "LONG" else -1),
                "stop_loss": _number(stop_loss, "stop_invalid", positive=True),
                "reference_price": _number(entry_price, "entry_invalid", positive=True),
                "account_id": snapshot["account_id"], **verdict}
    except Exception as error:
        return defer(str(error) or type(error).__name__)


def begin_sim_submission(preflight, *, client_order_id=None, instruction_id=None):
    """Caller-thread fsync BEFORE SDK send; no network/DB transaction overlaps."""
    guard = _guard()
    guard.submission_identity = None
    if guard.marker() is not None:
        raise BudgetUnavailable("submission_marker_already_exists")
    marker = {key: preflight[key] for key in
              ("account_id", "source", "symbol", "signed_qty", "stop_loss", "reference_price")}
    guard.submission_identity = uuid.uuid4().hex
    marker.update({"env": "sim", "identity": guard.submission_identity,
                   "client_order_id": client_order_id, "instruction_id": instruction_id,
                   "order_id": "", "created_at": datetime.now(timezone.utc).isoformat()})
    guard.write_marker(marker)


def observe_sim_submission(order_id):
    guard = _guard()
    marker = guard.marker()
    if marker is None:
        raise BudgetUnavailable("submission_marker_missing")
    oid = str(order_id or "")
    if marker.get("order_id") and marker["order_id"] != oid:
        raise BudgetUnavailable("submission_identity_conflict")
    if oid:
        marker["order_id"] = oid
        guard.write_marker(marker)


def reject_sim_submission(*, submit_state, dealt_qty):
    """Only caller-held, explicit definitive zero execution proof may release."""
    if submit_state == "DEFINITIVE_REJECT" and _number(dealt_qty, "dealt_unknown") == 0:
        guard = _guard()
        marker = guard.marker()
        if marker is not None and guard.submission_identity and marker.get("identity") == guard.submission_identity:
            guard.clear_marker()


def finish_sim_submission(owner, *, db_path=None):
    """Retain the slot until fresh exact terminal receipt and booking proof agree."""
    try:
        guard = _guard()
        marker = guard.marker()
        if marker is None:
            return True
        local = _read_local_truth(db_path)
        symbols = sorted({marker["symbol"]} | {row["symbol"] for row in local["trades"]})
        from app.playbook.entry_data_fetch import fetch_entry_sim_budget_snapshot
        snapshot = fetch_entry_sim_budget_snapshot(
            owner, symbols, expected_account_id=marker["account_id"], marker_receipt=marker)
        local = _read_local_truth(db_path)
        _snapshot_equity(snapshot, marker["account_id"])
        _portfolio(local, snapshot["positions"])
        _reconcile_marker(guard, snapshot, local)
        return guard.marker() is None
    except Exception:
        return False
