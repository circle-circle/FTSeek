#!/usr/bin/env python3
"""ET-calendar session gate and observation metrics for extended-hours exits."""
import json
import os
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


def _snapshot_field(snapshot, key):
    """快照取字段 — **绝不对 snapshot 做真值判定**。

    2026-09-19 P0 (延长时段退出链在"有持仓"时每分钟崩): futu 快照行走 `snap.iterrows()`,
    传给本模块的是 **pandas Series**, 而旧写法 `snapshot.get(key) if snapshot else None`
    里的 `if snapshot` 会走 `Series.__bool__` → `ValueError: The truth value of a Series
    is ambiguous`。崩点在 collect() 的第一行 (试算可执行价), 于是**只要有持仓**:
    退出决策/观测 jsonl/收口强平/next_open 兑现全部不执行, 且每分钟一轮零告警。
    无持仓时该路径不被触达 ⇒ 长期被"无持仓"掩盖 (9/18 09:38 起 next_open 7 次 +
    盘后 16:32 起每分钟)。修法 = 单点 None 判定, 覆盖 3 个调用方 (盘前/盘后/next_open)。
    """
    if snapshot is None:
        return None
    try:
        return snapshot.get(key)
    except Exception:
        return None


def quote_metrics(snapshot, direction, now=None):
    bid = _number(_snapshot_field(snapshot, "bid_price"))
    ask = _number(_snapshot_field(snapshot, "ask_price"))
    mid = (bid + ask) / 2 if bid is not None and ask is not None else None
    spread = ask - bid if bid is not None and ask is not None else None
    spread_pct = spread / mid * 100 if spread is not None and mid else None
    long_position = str(direction or "").upper() not in {"SHORT", "SELL_SHORT", "SELL"}
    executable = bid if long_position else ask
    quote_time = _parse_quote_time(_snapshot_field(snapshot, "update_time"))
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


def post_entry_high(symbol, entry_time, quote_ctx=None, max_days=5, kline_fn=None):
    """**进场之后**的真实最高价 (1m bar high, 只取 bar 时间 >= 进场时刻)。

    用户令 2026-09-19: 峰值口径只能用"入场之后"的高点 —— 时段/日内 high 字段
    (pre_high_price / after_high_price / high_price) 是**全时段累计**值, 含**入场前**的
    高点。若直接当峰值, 保护线会被推到"从未可成交"的位置 (假峰值 ⇒ 虚高捕获率 +
    虚假即时出场)。本函数按 bar 时间戳过滤, 天然只用进场后数据。

    取不到数据返回 None —— 调用方保持原峰值口径, **绝不阻断退出链**。
    """
    import datetime as _dt
    entry_dt = _parse_quote_time(entry_time) if isinstance(entry_time, str) else entry_time
    if entry_dt is None:
        return None
    et = ZoneInfo("America/New_York")
    if entry_dt.tzinfo is None:
        entry_dt = entry_dt.replace(tzinfo=et)
    today = _dt.datetime.now(et).date()
    days, cursor = [], min(entry_dt.date(), today)
    while cursor <= today and len(days) < max_days:
        days.append(cursor)
        cursor += _dt.timedelta(days=1)
    best = None
    for day in days:
        stamp = day.strftime("%Y-%m-%d")
        try:
            if kline_fn is not None:
                bars = kline_fn(stamp, stamp)
            else:
                from futu import KLType, RET_OK
                ret, kl, _ = quote_ctx.request_history_kline(
                    symbol, start=stamp, end=stamp, ktype=KLType.K_1M,
                    autype=None, max_count=1000, extended_time=True)
                bars = [] if ret != RET_OK or kl is None or len(kl) == 0 else [
                    {"t": str(row["time_key"]), "h": float(row["high"])}
                    for _, row in kl.iterrows()
                ]
        except Exception:
            bars = []
        for bar in bars:
            bar_dt = _parse_quote_time(bar["t"])
            if bar_dt is None:
                continue
            if bar_dt.tzinfo is None:
                bar_dt = bar_dt.replace(tzinfo=et)
            if bar_dt < entry_dt:
                continue          # ← 进场前的高点一律丢弃 (用户令 2026-09-19)
            if best is None or bar["h"] > best:
                best = bar["h"]
    return best


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


def _tail_lines(path, max_bytes=65536):
    """只读文件尾部 (2026-09-18 复查 F5: 原实现每次全量 read_text + 倒读 ⇒ 分钟级 cron × 每标的
    会随样本量线性放大 I/O)。同 session 的"上一行"必然在文件末尾附近 (按时间追加), 故尾部足够。"""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            start = max(0, size - max_bytes)
            handle.seek(start)
            data = handle.read()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="ignore").splitlines()
    if start > 0 and lines:
        lines = lines[1:]          # 丢掉可能被字节窗口截断的首行
    return lines


def previous_session_volume(output_path, symbol, trade_date, session):
    path = Path(output_path)
    if not path.exists():
        return None
    lines = _tail_lines(path)
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
