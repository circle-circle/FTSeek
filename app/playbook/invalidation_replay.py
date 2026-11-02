"""A1 offline invalidation replay primitives (research-only, zero actions).

This module is deliberately not imported by any runtime path.  It compiles no
orders and writes no database rows.  It exists to compare, on historical 1min
bars, a hard-stop/actual-exit baseline against two review-only invalidation
arms: structured close-price triggers and legacy text price triggers.

Pre-registered replay protocol (2026-10-28, DEB-20261028 §16):

* Entry bar is skipped.  A fill must be observable on a later bar.
* Intrabar trigger fill = next bar open + adverse slippage.  If the bar opens
  through the trigger, the open is used (it is already observable).
* Primary close confirmation = close of the bar 10 minutes before session
  close (15:50 ET normal / 12:50 ET early close), fill at next bar open.  This
  is a near-close proxy, not the official 16:00 close.
* Sensitivity close confirmation = official session close, fill at next
  session open.  This charges the overnight gap and is intentionally harsh.
* Hard SL gap = bar open + adverse slippage.  Hard SL touch = SL level +
  adverse slippage.  No SL level is ever treated as a risk-free fill.
* If SL and an intraday invalidation are both touched in the same 1min bar,
  the primary is pessimistic (SL first) and the sensitivity is optimistic
  (invalidation first).  A close-price confirmation is always after an SL
  touch in the same bar.
* Baseline = initial hard SL if touched, otherwise the actual exit.  This is
  an actual-exit-anchored counterfactual, not a replay of every trail/TP/time
  rule.  Both arms use the fee model on the same entry notional.

Units:
    prices are absolute; ``net_bps`` is net PnL divided by entry notional;
    ``delta_bps`` is arm minus baseline in bps.  Positive delta = invalidation
    arm is better.
"""
from __future__ import annotations

import json
import math
import re
import statistics
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from app.playbook.invalidation_contract import compile_structured_rules, parse_json_object
from app.utils.holiday_calendar import get_us_close_minutes_et
from app.utils.timezone import ET, to_et


REPLAY_SCHEMA = "invalidation_replay_v1"
SUMMARY_SCHEMA = "invalidation_replay_summary_v1"
PRIMARY_CLOSE_MODE = "near_close"
SENSITIVITY_CLOSE_MODE = "next_open"
PRIMARY_TRIGGER_ORDER = "pess"
SENSITIVITY_TRIGGER_ORDER = "opt"
DEFAULT_SLIPPAGE_BPS = 3.0
LEFT_TAIL_NET_BPS = -100.0
NEAR_CLOSE_BUFFER_MIN = 10
MDE80_Z = 1.959963984540054 + 0.8416212335729143

_ENTRY_CANCEL_RE = re.compile(
    r"(?:取消(?:建仓|所有买单|全部买单|未成交买单|挂单|入场|交易计划|交易)|"
    r"放弃本次交易|暂停(?:建仓|入场)|观望)"
)
_EXPLICIT_EXIT_RE = re.compile(
    r"(?:止损|平仓|清仓|终止交易|立即终止|逻辑失效|证伪|失效|离场)"
)
_DOWN_RE = re.compile(
    r"(?:跌破|跌穿|下破|低于|失守|回落至|未(?:能)?\s*(?:站稳|站上|站回|收复))"
)
_UP_RE = re.compile(r"(?:突破|上破|站上|站稳|涨破|站回|收复|高于|升破)")
_DELAY_RE = re.compile(
    r"(?:连续\s*\d+\s*(?:日|天|个交易日)|次日.{0,6}(?:收回|收复)|"
    r"未.{0,6}(?:收回|收复)|无法收回|站稳.{0,8}分钟|分钟以上)"
)
_COMPOUND_RE = re.compile(
    r"(?:且|并且|同时|伴随|放量|成交量|量比|资金|大单|净流出|换手率|"
    r"成交额|量能|缩量)"
)
_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _finite_float(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def parse_et_timestamp(value: Any) -> Optional[datetime]:
    """Parse DB timestamps with the repository rule: aware→ET, naive→CST."""
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is not None:
        return dt.astimezone(ET)
    return to_et(dt)


def _parse_bar_time(value: Any) -> Optional[datetime]:
    """Futu bar timestamps are ET wall-clock values, not server CST."""
    if isinstance(value, datetime):
        dt = value
        return dt.astimezone(ET) if dt.tzinfo is not None else _localize_et(dt)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return _localize_et(dt)


def _localize_et(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        return dt.astimezone(ET)
    if hasattr(ET, "localize"):
        return ET.localize(dt)
    return dt.replace(tzinfo=ET)


def _direction(value: Any) -> str:
    text = str(value or "").upper()
    if text in ("SHORT", "SELL_SHORT", "SELL"):
        return "SHORT"
    if text in ("LONG", "BUY"):
        return "LONG"
    return text


def _number_tokens(text: str) -> List[float]:
    out: List[float] = []
    for match in _NUM_RE.finditer(text):
        value = _finite_float(match.group(0).replace(",", ""))
        if value is not None:
            out.append(value)
    return out


def _local_clause(text: str, value: float) -> str:
    clauses = [part.strip() for part in re.split(r"[；;。\n]+", text or "") if part.strip()]
    for clause in clauses:
        if any(abs(token - value) <= max(1e-6, abs(value) * 1e-9)
               for token in _number_tokens(clause)):
            return clause
    return str(text or "").strip()


def classify_text_price_rule(text: Any, value: Any, direction: Any) -> Dict[str, Any]:
    """Classify the *selected* text price by its local clause.

    The A0 compiler only guarantees that the number is price-shaped.  This
    classifier is deliberately separate and conservative: cancellation-only
    clauses and directionally impossible clauses are not primary exit triggers.
    """
    raw_text = str(text or "").strip()
    number = _finite_float(value)
    side = _direction(direction)
    if not raw_text or number is None:
        return {
            "semantic_status": "invalid",
            "condition_class": "invalid",
            "local_clause": raw_text,
        }
    clause = _local_clause(raw_text, number)
    entry_cancel = bool(_ENTRY_CANCEL_RE.search(clause))
    explicit_exit = bool(_EXPLICIT_EXIT_RE.search(clause))
    down = bool(_DOWN_RE.search(clause))
    up = bool(_UP_RE.search(clause))

    if entry_cancel and not explicit_exit:
        status, klass = "non_exit", "non_exit_cancel"
    elif side == "LONG" and up and not down:
        status, klass = "ambiguous", "ambiguous_up_long"
    elif side == "SHORT" and down and not up:
        status, klass = "ambiguous", "ambiguous_down_short"
    elif _DELAY_RE.search(clause):
        status, klass = "exit", "confirmation_delay"
    elif _COMPOUND_RE.search(clause):
        status, klass = "exit", "compound"
    else:
        status, klass = "exit", "simple"
    return {
        "semantic_status": status,
        "condition_class": klass,
        "local_clause": clause,
        "entry_cancel_terms": entry_cancel,
        "explicit_exit_terms": explicit_exit,
        "down_terms": down,
        "up_terms": up,
    }


def select_structured_close_trigger(
    raw_instruction_json: Any, direction: Any
) -> Optional[Dict[str, Any]]:
    """Select the first-to-trigger structured close-price rule (OR semantics)."""
    rules, rejected, raw_count = compile_structured_rules(raw_instruction_json)
    side = _direction(direction)
    relevant: List[Dict[str, Any]] = []
    for rule in rules:
        if rule.get("metric") != "收盘价":
            continue
        op = rule.get("operator")
        if side == "LONG" and op == "<":
            relevant.append(rule)
        elif side == "SHORT" and op == ">":
            relevant.append(rule)
    if not relevant:
        return None
    chosen = (max(relevant, key=lambda item: float(item["value"]))
              if side == "LONG" else min(relevant, key=lambda item: float(item["value"])))
    return {
        **chosen,
        "source": "structured",
        "price_basis": "close",
        "semantic_status": "exit",
        "condition_class": (
            "structured_price_only" if raw_count == 1 else "structured_price_or_other"
        ),
        "structured_raw_count": raw_count,
        "structured_rejected": rejected,
    }


def normalise_bars(raw_bars: Iterable[Any]) -> Tuple[List[Dict[str, Any]], int]:
    """Return sorted ET-aware bars and a bad-row count."""
    bars: List[Dict[str, Any]] = []
    bad = 0
    for raw in raw_bars or []:
        try:
            if isinstance(raw, Mapping):
                t = _parse_bar_time(raw.get("t") or raw.get("time_key"))
                o = float(raw["o"] if "o" in raw else raw["open"])
                h = float(raw["h"] if "h" in raw else raw["high"])
                low = float(raw["l"] if "l" in raw else raw["low"])
                c = float(raw["c"] if "c" in raw else raw["close"])
            else:
                t = _parse_bar_time(raw[0])
                o, h, low, c = (float(raw[1]), float(raw[2]),
                                float(raw[3]), float(raw[4]))
        except (KeyError, IndexError, TypeError, ValueError):
            bad += 1
            continue
        if t is None or min(o, h, low, c) <= 0:
            bad += 1
            continue
        bars.append({"t": t, "o": o, "h": h, "l": low, "c": c})
    bars.sort(key=lambda item: item["t"])
    return bars, bad


def _day_close_minute(day: date) -> int:
    try:
        return int(get_us_close_minutes_et(day))
    except Exception:
        return 16 * 60


def _minute_of_day(dt: datetime) -> int:
    return dt.hour * 60 + dt.minute


def _near_close_bar(day_bars: Sequence[Mapping[str, Any]], day: date) -> Optional[Mapping[str, Any]]:
    target = _day_close_minute(day) - NEAR_CLOSE_BUFFER_MIN
    eligible = [bar for bar in day_bars if _minute_of_day(bar["t"]) <= target]
    return eligible[-1] if eligible else None


def _last_session_bar(day_bars: Sequence[Mapping[str, Any]], day: date) -> Optional[Mapping[str, Any]]:
    close_minute = _day_close_minute(day)
    eligible = [bar for bar in day_bars if _minute_of_day(bar["t"]) <= close_minute]
    return eligible[-1] if eligible else None


def _slip_price(price: float, direction: str, slippage_bps: float) -> float:
    slip = max(0.0, float(slippage_bps or 0.0)) / 10000.0
    return price * (1.0 - slip) if direction == "LONG" else price * (1.0 + slip)


def _sl_event(bar: Mapping[str, Any], direction: str, stop_loss: Optional[float]) -> Optional[Dict[str, Any]]:
    sl = _finite_float(stop_loss)
    if sl is None or sl <= 0:
        return None
    open_price = float(bar["o"])
    if direction == "LONG":
        if open_price <= sl:
            return {"kind": "SL_GAP", "price": open_price}
        if float(bar["l"]) <= sl:
            return {"kind": "SL_TOUCH", "price": sl}
    else:
        if open_price >= sl:
            return {"kind": "SL_GAP", "price": open_price}
        if float(bar["h"]) >= sl:
            return {"kind": "SL_TOUCH", "price": sl}
    return None


def _trigger_price_event(
    bar: Mapping[str, Any], direction: str, operator: str, value: float
) -> Dict[str, bool]:
    if direction == "LONG":
        gap = operator == "<" and float(bar["o"]) <= value
        touch = operator == "<" and float(bar["l"]) <= value
    else:
        gap = operator == ">" and float(bar["o"]) >= value
        touch = operator == ">" and float(bar["h"]) >= value
    return {"gap": bool(gap), "touch": bool(touch)}


def _close_condition(close_price: float, direction: str, operator: str, value: float) -> bool:
    if direction == "LONG":
        return operator == "<" and close_price < value
    return operator == ">" and close_price > value


class FeeModelError(RuntimeError):
    """Raised when the fee model cannot price a leg."""


def _fee_for_leg(
    qty: float,
    entry_price: float,
    exit_price: float,
    direction: str,
    entry_day: date,
    exit_day: date,
) -> float:
    try:
        from app.execution.fee_model import estimate_round_trip
        return float(estimate_round_trip(
            qty, entry_price, exit_price, direction,
            trade_date=entry_day.isoformat(), exit_date=exit_day.isoformat(),
        )["total"])
    except Exception as exc:
        raise FeeModelError(
            f"fee model failed qty={qty} entry={entry_price} exit={exit_price} "
            f"dir={direction} {entry_day}->{exit_day}: {exc}"
        ) from exc


def _leg(
    *,
    qty: float,
    entry_price: float,
    exit_price: float,
    direction: str,
    entry_day: date,
    exit_time: datetime,
    exit_reason: str,
    simulated: bool,
) -> Dict[str, Any]:
    sign = 1.0 if direction == "LONG" else -1.0
    gross = qty * sign * (exit_price - entry_price)
    fee = 0.0
    fee_error: Optional[str] = None
    try:
        fee = _fee_for_leg(
            qty, entry_price, exit_price, direction, entry_day, exit_time.date()
        )
    except FeeModelError as exc:
        fee_error = str(exc)
    net = gross - fee
    notional = qty * entry_price
    return {
        "exit_price": round(exit_price, 6),
        "exit_time": exit_time.isoformat(),
        "exit_date": exit_time.date().isoformat(),
        "exit_reason": exit_reason,
        "simulated_fill": bool(simulated),
        "gross_pnl": round(gross, 4),
        "fee": round(fee, 4),
        "fee_error": fee_error,
        "net_pnl": round(net, 4),
        "net_bps": round(net / notional * 10000.0, 4) if notional > 0 else None,
    }


def _actual_leg(
    trade: Mapping[str, Any], direction: str, entry_price: float, qty: float,
    entry_time: datetime, exit_time: datetime, exit_price: float,
) -> Dict[str, Any]:
    return _leg(
        qty=qty,
        entry_price=entry_price,
        exit_price=exit_price,
        direction=direction,
        entry_day=entry_time.date(),
        exit_time=exit_time,
        exit_reason="ACTUAL_EXIT",
        simulated=False,
    )


def _is_before_exit(bar_time: datetime, exit_time: datetime) -> bool:
    """A bar may be used only if its start is strictly before actual exit."""
    return bar_time < exit_time


def simulate_invalidation_trade(
    *,
    trade: Mapping[str, Any],
    bars: Sequence[Mapping[str, Any]],
    trigger: Mapping[str, Any],
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
    trigger_order: str = PRIMARY_TRIGGER_ORDER,
    close_mode: str = PRIMARY_CLOSE_MODE,
) -> Dict[str, Any]:
    """Replay one trade under baseline and one invalidation arm."""
    if trigger_order not in ("pess", "opt"):
        raise ValueError("trigger_order must be pess or opt")
    if close_mode not in (PRIMARY_CLOSE_MODE, SENSITIVITY_CLOSE_MODE):
        raise ValueError("close_mode must be near_close or next_open")

    direction = _direction(trade.get("direction"))
    entry_time = trade.get("entry_et")
    exit_time = trade.get("exit_et")
    if not isinstance(entry_time, datetime):
        entry_time = parse_et_timestamp(entry_time)
    if not isinstance(exit_time, datetime):
        exit_time = parse_et_timestamp(exit_time)
    entry_price = _finite_float(trade.get("entry_price"))
    exit_price = _finite_float(trade.get("exit_price"))
    qty = _finite_float(trade.get("quantity"))
    if (direction not in ("LONG", "SHORT") or entry_time is None or exit_time is None
            or entry_price is None or entry_price <= 0 or exit_price is None
            or exit_price <= 0 or qty is None or qty <= 0 or exit_time <= entry_time):
        return {"status": "bad_trade_input", "trade_id": trade.get("id")}

    usable = [bar for bar in bars
              if _is_before_exit(bar["t"], exit_time) and bar["t"] > entry_time]
    if not usable:
        return {"status": "no_usable_bars", "trade_id": trade.get("id")}

    sl = _finite_float(trade.get("stop_loss"))
    actual = _actual_leg(trade, direction, entry_price, qty, entry_time, exit_time, exit_price)
    baseline: Optional[Dict[str, Any]] = None
    for bar in usable:
        event = _sl_event(bar, direction, sl)
        if event:
            fill = _slip_price(float(event["price"]), direction, slippage_bps)
            baseline = _leg(
                qty=qty, entry_price=entry_price, exit_price=fill,
                direction=direction, entry_day=entry_time.date(),
                exit_time=bar["t"], exit_reason=event["kind"], simulated=True,
            )
            break
    if baseline is None:
        baseline = dict(actual)

    value = _finite_float(trigger.get("value"))
    operator = str(trigger.get("operator") or "")
    basis = str(trigger.get("price_basis") or "intraday")
    if value is None or value <= 0 or operator not in ("<", ">"):
        return {
            "status": "bad_trigger", "trade_id": trade.get("id"),
            "baseline": baseline,
        }

    deadline_text = str(trigger.get("deadline") or "")[:10]
    try:
        deadline = date.fromisoformat(deadline_text) if deadline_text else None
    except ValueError:
        deadline = None

    # Use the full session for close-confirmation lookup.  Using only bars
    # before the actual exit would silently turn the last available bar of an
    # early-exit day into a fake "near-close" bar (e.g. a 10:00 exit becoming a
    # 15:50 confirmation), inflating trigger_observed.
    bars_by_day: Dict[date, List[Mapping[str, Any]]] = {}
    for bar in bars:
        bars_by_day.setdefault(bar["t"].date(), []).append(bar)

    observed = False
    applied = False
    unusable_reason: Optional[str] = None
    arm: Optional[Dict[str, Any]] = None
    trigger_bar: Optional[Mapping[str, Any]] = None
    fill_bar: Optional[Mapping[str, Any]] = None

    if basis == "close":
        for index, bar in enumerate(usable):
            day = bar["t"].date()
            if deadline is not None and day > deadline:
                continue
            sl_event = _sl_event(bar, direction, sl)
            if sl_event:
                arm = dict(baseline)
                break
            day_bars = bars_by_day.get(day) or []
            confirmation = (_near_close_bar(day_bars, day) if close_mode == PRIMARY_CLOSE_MODE
                            else _last_session_bar(day_bars, day))
            if confirmation is None or confirmation["t"] != bar["t"]:
                continue
            if not _close_condition(float(bar["c"]), direction, operator, value):
                continue
            observed = True
            trigger_bar = bar
            next_bar = usable[index + 1] if index + 1 < len(usable) else None
            if next_bar is None or not _is_before_exit(next_bar["t"], exit_time):
                unusable_reason = "no_fill_before_actual_exit"
                arm = dict(baseline)
                break
            fill = _slip_price(float(next_bar["o"]), direction, slippage_bps)
            fill_bar = next_bar
            arm = _leg(
                qty=qty, entry_price=entry_price, exit_price=fill,
                direction=direction, entry_day=entry_time.date(),
                exit_time=next_bar["t"], exit_reason="INVALIDATION_CLOSE", simulated=True,
            )
            applied = True
            break
    else:
        for index, bar in enumerate(usable):
            day = bar["t"].date()
            if deadline is not None and day > deadline:
                continue
            sl_event = _sl_event(bar, direction, sl)
            if sl_event and sl_event["kind"] == "SL_GAP":
                arm = dict(baseline)
                break
            event = _trigger_price_event(bar, direction, operator, value)
            if event["gap"]:
                observed = True
                trigger_bar = bar
                fill = _slip_price(float(bar["o"]), direction, slippage_bps)
                fill_bar = bar
                arm = _leg(
                    qty=qty, entry_price=entry_price, exit_price=fill,
                    direction=direction, entry_day=entry_time.date(),
                    exit_time=bar["t"], exit_reason="INVALIDATION_INTRADAY", simulated=True,
                )
                applied = True
                break
            if event["touch"]:
                observed = True
                trigger_bar = bar
                if sl_event and trigger_order == "pess":
                    arm = dict(baseline)
                    break
                next_bar = usable[index + 1] if index + 1 < len(usable) else None
                if next_bar is None or not _is_before_exit(next_bar["t"], exit_time):
                    unusable_reason = "no_fill_before_actual_exit"
                    arm = dict(baseline)
                    break
                fill = _slip_price(float(next_bar["o"]), direction, slippage_bps)
                fill_bar = next_bar
                arm = _leg(
                    qty=qty, entry_price=entry_price, exit_price=fill,
                    direction=direction, entry_day=entry_time.date(),
                    exit_time=next_bar["t"], exit_reason="INVALIDATION_INTRADAY", simulated=True,
                )
                applied = True
                break
            if sl_event:
                arm = dict(baseline)
                break

    if arm is None:
        arm = dict(baseline)

    delta_net = float(arm["net_pnl"]) - float(baseline["net_pnl"])
    delta_gross = float(arm["gross_pnl"]) - float(baseline["gross_pnl"])
    notional = qty * entry_price
    return {
        "status": "ok",
        "trade_id": trade.get("id"),
        "symbol": trade.get("symbol"),
        "direction": direction,
        "entry_time": entry_time.isoformat(),
        "exit_time": exit_time.isoformat(),
        "entry_price": entry_price,
        "exit_price": exit_price,
        "quantity": qty,
        "notional": round(notional, 4),
        "bucket_effective": trade.get("bucket_effective"),
        "trigger": dict(trigger),
        "trigger_observed": observed,
        "trigger_applied": applied,
        "trigger_unusable_reason": unusable_reason,
        "trigger_bar": trigger_bar["t"].isoformat() if trigger_bar else None,
        "fill_bar": fill_bar["t"].isoformat() if fill_bar else None,
        "baseline": baseline,
        "arm": arm,
        "delta_gross": round(delta_gross, 4),
        "delta_net": round(delta_net, 4),
        "delta_bps": round(delta_net / notional * 10000.0, 4) if notional > 0 else None,
        "slippage_bps": float(slippage_bps),
        "trigger_order": trigger_order,
        "close_mode": close_mode,
        "schema_version": REPLAY_SCHEMA,
    }


def daily_cluster_stats(values_by_day: Mapping[str, Sequence[float]]) -> Dict[str, Any]:
    """Equal-weight daily means → clustered mean/t/95% CI/MDE80."""
    daily_means = [
        statistics.fmean(float(v) for v in values)
        for _day, values in sorted(values_by_day.items()) if values
    ]
    n_days = len(daily_means)
    if n_days == 0:
        return {"days": 0, "mean": None, "t": None, "ci95_low": None,
                "ci95_high": None, "mde80": None}
    mean = statistics.fmean(daily_means)
    if n_days < 2:
        return {"days": 1, "mean": round(mean, 6), "t": None,
                "ci95_low": None, "ci95_high": None, "mde80": None}
    sd = statistics.stdev(daily_means)
    se = sd / math.sqrt(n_days)
    if se == 0:
        return {"days": n_days, "mean": round(mean, 6), "t": None,
                "ci95_low": round(mean, 6), "ci95_high": round(mean, 6), "mde80": 0.0}
    return {
        "days": n_days,
        "mean": round(mean, 6),
        "t": round(mean / se, 4),
        "ci95_low": round(mean - 1.959963984540054 * se, 6),
        "ci95_high": round(mean + 1.959963984540054 * se, 6),
        "mde80": round(MDE80_Z * se, 6),
    }


def _baseline_had_near_close_window(row: Mapping[str, Any]) -> bool:
    """Whether the baseline holding period crossed a 10-min-before-close mark."""
    try:
        entry = datetime.fromisoformat(str(row.get("entry_time") or ""))
        exit_ = datetime.fromisoformat(str((row.get("baseline") or {}).get("exit_time") or ""))
    except ValueError:
        return False
    if exit_.date() > entry.date():
        return True
    target = _day_close_minute(exit_.date()) - NEAR_CLOSE_BUFFER_MIN
    return exit_.hour * 60 + exit_.minute > target


def summarize_arm(rows: Sequence[Mapping[str, Any]], *, arm_label: str) -> Dict[str, Any]:
    """Paired baseline-vs-arm summary with daily clustering and MDE."""
    if not rows:
        return {"arm": arm_label, "n": 0}
    base_net = [float(r["baseline"]["net_pnl"]) for r in rows]
    arm_net = [float(r["arm"]["net_pnl"]) for r in rows]
    base_gross = [float(r["baseline"]["gross_pnl"]) for r in rows]
    arm_gross = [float(r["arm"]["gross_pnl"]) for r in rows]
    base_bps = [float(r["baseline"].get("net_bps") or 0.0) for r in rows]
    arm_bps = [float(r["arm"].get("net_bps") or 0.0) for r in rows]
    deltas_net = [a - b for a, b in zip(arm_net, base_net)]
    deltas_gross = [a - b for a, b in zip(arm_gross, base_gross)]
    deltas_bps = [a - b for a, b in zip(arm_bps, base_bps)]
    triggered = [r for r in rows if r.get("trigger_applied")]
    near_close_rows = [r for r in rows if _baseline_had_near_close_window(r)]
    near_close_triggered = [r for r in near_close_rows if r.get("trigger_applied")]

    by_day_net: Dict[str, List[float]] = {}
    by_day_usd: Dict[str, List[float]] = {}
    for row, delta_net, delta_bps in zip(rows, deltas_net, deltas_bps):
        day = str(row.get("entry_time") or "")[:10]
        by_day_net.setdefault(day, []).append(delta_bps)
        by_day_usd.setdefault(day, []).append(delta_net)
    cluster_bps = daily_cluster_stats(by_day_net)
    cluster_usd = daily_cluster_stats(by_day_usd)

    avoided = [(b, a) for b, a in zip(base_net, arm_net) if b < 0 and a > b]
    killed = [(b, a) for b, a in zip(base_net, arm_net) if b > 0 and a <= 0]
    left_tail_base = [b for b, bp in zip(base_net, base_bps) if bp <= LEFT_TAIL_NET_BPS]
    left_tail_arm = [a for a, bp in zip(arm_net, base_bps) if bp <= LEFT_TAIL_NET_BPS]
    left_tail_base_gross = [b for b, bp in zip(base_gross, base_bps) if bp <= LEFT_TAIL_NET_BPS]
    left_tail_arm_gross = [a for a, bp in zip(arm_gross, base_bps) if bp <= LEFT_TAIL_NET_BPS]
    right_tail_pairs = [(b, a) for b, a, bp in zip(base_net, arm_net, base_bps)
                        if bp >= -LEFT_TAIL_NET_BPS]
    right_tail_base = [b for b, _a in right_tail_pairs]
    right_tail_arm = [a for _b, a in right_tail_pairs]
    winner_pairs = [(b, a) for b, a in zip(base_net, arm_net) if b > 0]
    triggered_deltas = [
        float(r.get("delta_net", float(r["arm"]["net_pnl"]) - float(r["baseline"]["net_pnl"])))
        for r in triggered
    ]
    triggered_bps = [
        float(r.get("delta_bps", float(r["arm"].get("net_bps") or 0.0)
                    - float(r["baseline"].get("net_bps") or 0.0)))
        for r in triggered
    ]
    win_deltas = [d for d in deltas_net if d > 0]
    loss_deltas = [d for d in deltas_net if d < 0]
    fee_error_rows = [
        r for r in rows
        if (r.get("baseline") or {}).get("fee_error")
        or (r.get("arm") or {}).get("fee_error")
    ]

    return {
        "arm": arm_label,
        "n": len(rows),
        "n_triggered": len(triggered),
        "trigger_rate": round(len(triggered) / len(rows), 4),
        "near_close_window_n": len(near_close_rows),
        "near_close_window_trigger_rate": (
            round(len(near_close_triggered) / len(near_close_rows), 4)
            if near_close_rows else None
        ),
        "daily_cluster": cluster_bps,
        "daily_cluster_usd": cluster_usd,
        "mde80_net_bps": cluster_bps.get("mde80"),
        "mde80_net_usd": cluster_usd.get("mde80"),
        "baseline_net_sum": round(sum(base_net), 2),
        "arm_net_sum": round(sum(arm_net), 2),
        "delta_net_sum": round(sum(deltas_net), 2),
        "baseline_gross_sum": round(sum(base_gross), 2),
        "arm_gross_sum": round(sum(arm_gross), 2),
        "delta_gross_sum": round(sum(deltas_gross), 2),
        "avg_delta_net": round(statistics.fmean(deltas_net), 4),
        "avg_delta_bps": round(statistics.fmean(deltas_bps), 4),
        "median_delta_bps": round(statistics.median(deltas_bps), 4),
        "avoided_loss_net_usd": round(sum(a - b for b, a in avoided), 2),
        "avoided_loss_n": len(avoided),
        "killed_winners_n": len(killed),
        "killed_winners_profit_usd": round(sum(b - a for b, a in killed), 2),
        "winner_profit_erosion_net_usd": round(
            sum(max(0.0, b - a) for b, a in winner_pairs), 2
        ),
        "winners_preserved_n": sum(a > 0 for _b, a in winner_pairs),
        "right_tail_n": len(right_tail_pairs),
        "right_tail_baseline_net_usd": round(sum(right_tail_base), 2),
        "right_tail_arm_net_usd": round(sum(right_tail_arm), 2),
        "right_tail_change_net_usd": round(sum(right_tail_arm) - sum(right_tail_base), 2),
        "right_tail_erosion_net_usd": round(
            sum(max(0.0, b - a) for b, a in right_tail_pairs), 2
        ),
        "left_tail_threshold_bps": LEFT_TAIL_NET_BPS,
        "left_tail_n": len(left_tail_base),
        "left_tail_baseline_net_usd": round(sum(left_tail_base), 2),
        "left_tail_arm_net_usd": round(sum(left_tail_arm), 2),
        "left_tail_reduction_net_usd": round(sum(left_tail_arm) - sum(left_tail_base), 2),
        "left_tail_baseline_gross_usd": round(sum(left_tail_base_gross), 2),
        "left_tail_arm_gross_usd": round(sum(left_tail_arm_gross), 2),
        "left_tail_reduction_gross_usd": round(
            sum(left_tail_arm_gross) - sum(left_tail_base_gross), 2
        ),
        "positive_delta_n": len(win_deltas),
        "negative_delta_n": len(loss_deltas),
        "triggered_delta_net_sum": round(sum(triggered_deltas), 2),
        "triggered_avg_delta_net": (
            round(statistics.fmean(triggered_deltas), 4) if triggered_deltas else None
        ),
        "triggered_avg_delta_bps": (
            round(statistics.fmean(triggered_bps), 4) if triggered_bps else None
        ),
        "triggered_positive_rate": (
            round(sum(value > 0 for value in triggered_deltas) / len(triggered_deltas), 4)
            if triggered_deltas else None
        ),
        "small_n_warning": len(rows) < 30,
        "n_fee_error_rows": len(fee_error_rows),
        "fee_error_sample": [
            str((r.get("baseline") or {}).get("fee_error")
                or (r.get("arm") or {}).get("fee_error"))
            for r in fee_error_rows[:3]
        ],
    }


def grouped_summaries(
    rows: Sequence[Mapping[str, Any]], key: str, *, arm_label: str
) -> Dict[str, Any]:
    groups: Dict[str, List[Mapping[str, Any]]] = {}
    for row in rows:
        value = row.get(key)
        label = str(value) if value not in (None, "") else "unknown"
        groups.setdefault(label, []).append(row)
    return {
        label: summarize_arm(group, arm_label=f"{arm_label}|{key}={label}")
        for label, group in sorted(groups.items())
    }


def trigger_semantics_counts(rows: Sequence[Mapping[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for row in rows:
        klass = str((row.get("trigger") or {}).get("condition_class") or "unknown")
        counts[klass] = counts.get(klass, 0) + 1
    return dict(sorted(counts.items()))


def trigger_status_counts(rows: Sequence[Mapping[str, Any]]) -> Dict[str, int]:
    counts = {
        "observed": 0,
        "applied": 0,
        "unusable_no_fill": 0,
    }
    for row in rows:
        if row.get("trigger_observed"):
            counts["observed"] += 1
        if row.get("trigger_applied"):
            counts["applied"] += 1
        if row.get("trigger_unusable_reason") == "no_fill_before_actual_exit":
            counts["unusable_no_fill"] += 1
    return counts
