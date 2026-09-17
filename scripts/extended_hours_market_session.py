#!/usr/bin/env python3
"""ET-calendar session gate and observation metrics for extended-hours exits."""
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


FIELD_UNITS = {
    "bid_price": "absolute_price",
    "ask_price": "absolute_price",
    "mid_price": "absolute_price",
    "executable_price": "absolute_price",
    "spread": "absolute_price",
    "spread_pct": "percent",
    "quote_age_seconds": "seconds",
    "session_volume": "shares",
    "session_volume_delta": "shares",
    "risk_r_pct": "percent",
    "atr_abs": "absolute_price",
    "atr_pct": "percent",
    "atr_source": "enum",
    "round_trip_fee_usd": "usd",
    "round_trip_fee_pct": "percent",
    "gross_executable_pnl_pct": "percent",
    "net_executable_pnl_pct": "percent",
}


def _minutes(hhmm):
    return (int(hhmm) // 100) * 60 + (int(hhmm) % 100)


def extended_hours_session_for(trade_date, hhmm):
    from app.utils.holiday_calendar import get_holiday_calendar, get_us_close_minutes_et

    from datetime import date
    parsed_date = date.fromisoformat(str(trade_date)[:10])
    if not get_holiday_calendar().is_trading_day("US", parsed_date):
        return None
    minutes = _minutes(hhmm)
    close_minutes = int(get_us_close_minutes_et(parsed_date))
    if 240 <= minutes < 570:
        return "premarket"
    if close_minutes <= minutes < 1200:
        return "afterhours"
    return None


def cheap_extended_hours_session_for(trade_date, hhmm):
    from datetime import date

    parsed_date = date.fromisoformat(str(trade_date)[:10])
    if parsed_date.weekday() >= 5:
        return None
    minutes = _minutes(hhmm)
    if 240 <= minutes < 570:
        return "premarket"
    from app.utils.holiday_calendar import US_EARLY_CLOSE_DATES

    close_minutes = 780 if parsed_date in US_EARLY_CLOSE_DATES else 960
    if close_minutes <= minutes < 1200:
        return "afterhours"
    return None


def get_extended_hours_session(now=None):
    if now is None:
        from app.utils.timezone import now_et
        now = now_et()
    hhmm = now.hour * 100 + now.minute
    if cheap_extended_hours_session_for(now.date(), hhmm) is None:
        return None
    return extended_hours_session_for(now.date(), hhmm)


def _number(value):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _parse_quote_time(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace(" ", "T"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("America/New_York"))
    return parsed


def quote_metrics(snapshot, direction, now=None):
    bid = _number(snapshot.get("bid_price")) if snapshot else None
    ask = _number(snapshot.get("ask_price")) if snapshot else None
    mid = (bid + ask) / 2 if bid is not None and ask is not None else None
    spread = ask - bid if bid is not None and ask is not None else None
    spread_pct = spread / mid * 100 if spread is not None and mid else None
    long_position = str(direction or "").upper() not in {"SHORT", "SELL_SHORT", "SELL"}
    executable = bid if long_position else ask
    quote_time = _parse_quote_time(snapshot.get("update_time")) if snapshot else None
    quote_age = None
    if quote_time is not None:
        if now is None:
            from app.utils.timezone import now_et
            now = now_et()
        quote_age = max(0.0, (now - quote_time).total_seconds())
    return {
        "bid_price": bid,
        "ask_price": ask,
        "mid_price": mid,
        "spread": spread,
        "spread_pct": round(spread_pct, 4) if spread_pct is not None else None,
        "executable_price": executable,
        "quote_age_seconds": round(quote_age, 3) if quote_age is not None else None,
    }


def risk_r_pct(entry_price, stop_price, direction):
    entry = _number(entry_price)
    stop = _number(stop_price)
    if entry is None or stop is None:
        return None
    short = str(direction or "").upper() in {"SHORT", "SELL_SHORT", "SELL"}
    risk = (stop - entry) if short else (entry - stop)
    if risk <= 0:
        return None
    return round(risk / entry * 100, 4)


def atr_metrics(entry_price, metadata):
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (TypeError, ValueError):
            metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    atr_abs = _number(metadata.get("atr"))
    entry = _number(entry_price)
    atr_pct = atr_abs / entry * 100 if atr_abs is not None and entry is not None else None
    return {
        "atr_abs": atr_abs,
        "atr_pct": round(atr_pct, 4) if atr_pct is not None else None,
        "atr_source": "trade_metadata_atr" if atr_abs is not None else None,
    }


def round_trip_fee_metrics(quantity, entry_price, exit_price, direction, trade_date):
    from app.execution.fee_model import estimate_round_trip

    quantity = float(quantity or 0)
    entry = _number(entry_price)
    exit_price = _number(exit_price)
    if quantity <= 0 or entry is None or exit_price is None:
        return None
    fees = estimate_round_trip(quantity, entry, exit_price, direction, trade_date)
    notional = quantity * entry
    return {
        "round_trip_fee_usd": round(float(fees["total"]), 4),
        "round_trip_fee_pct": round(float(fees["total"]) / notional * 100, 4),
    }


def net_executable_pnl_pct(entry_price, exit_price, direction, fee_pct):
    entry = _number(entry_price)
    exit_price = _number(exit_price)
    if entry is None or exit_price is None or fee_pct is None:
        return None
    sign = -1 if str(direction or "").upper() in {"SHORT", "SELL_SHORT", "SELL"} else 1
    gross = (exit_price - entry) / entry * 100 * sign
    return round(gross - float(fee_pct), 4)


def previous_session_volume(output_path, symbol, trade_date, session):
    path = Path(output_path)
    if not path.exists():
        return None
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except Exception:
            continue
        if (
            row.get("symbol") == symbol
            and row.get("trade_date") == trade_date
            and row.get("session") == session
        ):
            value = row.get("session_volume")
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
    return None


def session_volume_delta(output_path, symbol, trade_date, session, current_volume):
    previous = previous_session_volume(output_path, symbol, trade_date, session)
    try:
        current = float(current_volume)
    except (TypeError, ValueError):
        return None
    if previous is None:
        return None
    return round(current - previous, 0)
