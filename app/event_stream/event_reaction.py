# -*- coding: utf-8 -*-
"""事件后价格反应 (event_reaction_v1, 2026-10-21).

目的: 让 UE brief / DA 看到"这条事件多久以前"以及"事件后市场到底怎么走",
避免把 23h 前的旧闻当成当前利好/利空, 也避免只看新闻措辞而不看收盘方向。

边界:
  - 纯函数, 不触网/不写库; 日线与报价由调用方注入.
  - 日线一阶近似: 盘前/盘中事件 = 当日收盘 vs 事件前收盘; 盘后/隔夜事件 =
    次日盘前/开盘 vs 事件前收盘. 盘中事件用日线含事件前漂移, 这是已知偏差,
    由 reaction_basis 标注; 需要精确事件窗收益时另立 Phase 1 用 1m bar.
  - 任何缺失/异常返回 null + reason, 绝不猜价格/方向.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence
from zoneinfo import ZoneInfo

EVENT_REACTION_SCHEMA = "event_reaction_v1"
_ET = ZoneInfo("America/New_York")
_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


def _as_et(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                dt = parsedate_to_datetime(text)
            except (TypeError, ValueError, OverflowError):
                return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_ET)


def parse_event_time(value: Any) -> Optional[datetime]:
    """ISO/RFC822/aware datetime → ET aware datetime; 纯日期/非法 → None."""
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        return None  # 日期无时刻不猜盘中/盘后
    return _as_et(value)


def _calendar():
    try:
        from app.utils.holiday_calendar import get_holiday_calendar
        return get_holiday_calendar()
    except Exception:
        return None


def _is_trading_day(day: date, calendar=None) -> bool:
    if day.weekday() >= 5:
        return False
    if calendar is None:
        return True
    try:
        return bool(calendar.is_trading_day("US", day))
    except Exception:
        return True


def _close_minutes(day: date) -> int:
    try:
        from app.utils.holiday_calendar import get_us_close_minutes_et
        return int(get_us_close_minutes_et(day))
    except Exception:
        return 16 * 60


def _close_dt(day: date) -> datetime:
    mins = _close_minutes(day)
    return datetime.combine(day, time(mins // 60, mins % 60), tzinfo=_ET)


def _open_dt(day: date) -> datetime:
    return datetime.combine(day, time(9, 30), tzinfo=_ET)


def classify_event_session(event_time: datetime) -> str:
    """事件发生时的 ET 时段; 半日市按当日实际收盘, 周末/假日标 closed."""
    day = event_time.date()
    if not _is_trading_day(day, _calendar()):
        return "closed"
    mins = event_time.hour * 60 + event_time.minute
    close = _close_minutes(day)
    if 4 * 60 <= mins < 9 * 60 + 30:
        return "premarket"
    if 9 * 60 + 30 <= mins < close:
        return "regular"
    if close <= mins < 20 * 60:
        return "after_hours"
    return "overnight"


def _bar_date(bar: Dict[str, Any]) -> Optional[date]:
    raw = bar.get("time") or bar.get("date") or bar.get("time_key")
    if raw is None:
        return None
    text = str(raw)[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def _bar_num(bar: Dict[str, Any], key: str) -> Optional[float]:
    try:
        value = float(bar.get(key))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _bars_sorted(bars: Optional[Iterable[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    rows = [bar for bar in (bars or []) if isinstance(bar, dict) and _bar_date(bar)]
    rows.sort(key=lambda bar: _bar_date(bar))
    return rows


def _last_before(rows: Sequence[Dict[str, Any]], day: date) -> Optional[Dict[str, Any]]:
    hits = [bar for bar in rows if _bar_date(bar) < day]
    return hits[-1] if hits else None


def _at(rows: Sequence[Dict[str, Any]], day: date) -> Optional[Dict[str, Any]]:
    hits = [bar for bar in rows if _bar_date(bar) == day]
    return hits[-1] if hits else None


def _first_after(rows: Sequence[Dict[str, Any]], day: date) -> Optional[Dict[str, Any]]:
    hits = [bar for bar in rows if _bar_date(bar) > day]
    return hits[0] if hits else None


def _next_trading_day(day: date, calendar=None) -> date:
    cursor = day + timedelta(days=1)
    for _ in range(10):
        if _is_trading_day(cursor, calendar):
            return cursor
        cursor += timedelta(days=1)
    return day + timedelta(days=1)


def _quote_price(quote: Dict[str, Any], computed_at: datetime) -> Optional[float]:
    """盘前用 pre_price, 开盘后用 price; 缺失回退到另一侧."""
    q = quote if isinstance(quote, dict) else {}
    pre = _bar_num(q, "pre_price")
    regular = _bar_num(q, "price") or _bar_num(q, "last_price")
    if computed_at < _open_dt(computed_at.date()):
        return pre or regular
    return regular or pre


def _quote_asof(quote: Dict[str, Any]) -> Optional[str]:
    q = quote if isinstance(quote, dict) else {}
    for key in ("source_asof", "update_time"):
        value = q.get(key)
        if value:
            return str(value)
    return None


def _symbols(symbols: Iterable[Any], max_symbols: int) -> List[str]:
    out: List[str] = []
    for raw in symbols or []:
        symbol = str(raw or "").strip().upper().removeprefix("US.")
        if symbol and _SYMBOL_RE.fullmatch(symbol) and symbol not in out:
            out.append(symbol)
        if len(out) >= max_symbols:
            break
    return out


def _entry(
    *,
    age_h: Optional[float],
    session: Optional[str],
    basis: str = "unknown",
    pct: Optional[float] = None,
    reaction_at: Optional[datetime] = None,
    reaction_price: Optional[float] = None,
    reference_at: Optional[datetime] = None,
    reference_price: Optional[float] = None,
    source: str = "none",
    reason: Optional[str] = None,
    quote_asof: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "age_h": age_h,
        "event_session": session,
        "post_event_move_pct": (round(pct, 4) if pct is not None else None),
        "reaction_basis": (
            "pending" if basis == "unknown" and reason in (
                "premarket_not_started", "same_session_price_not_available")
            else basis),
        "reaction_at": reaction_at.isoformat() if isinstance(reaction_at, datetime) else None,
        "reaction_price": reaction_price,
        "reference_price": reference_price,
        "reference_at": reference_at.isoformat() if isinstance(reference_at, datetime) else None,
        "source": source,
        "reason": reason,
        "quote_asof": quote_asof,
    }


def _pct(reaction_price: Optional[float], reference_price: Optional[float]) -> Optional[float]:
    if reaction_price is None or reference_price is None or reference_price <= 0:
        return None
    return (float(reaction_price) / float(reference_price) - 1.0) * 100.0


def _resolve_quote(
    symbol: str,
    quote: Optional[Dict[str, Any]],
    provider: Optional[Callable[[str], Optional[Dict[str, Any]]]],
) -> Dict[str, Any]:
    if isinstance(quote, dict) and quote:
        return quote
    if provider is None:
        return {}
    try:
        fetched = provider(symbol)
    except Exception:
        return {}
    return fetched if isinstance(fetched, dict) else {}


def _compute_one(
    symbol: str,
    *,
    event_dt: datetime,
    session: str,
    computed_at: datetime,
    bars: Sequence[Dict[str, Any]],
    quote: Optional[Dict[str, Any]],
    age_h: Optional[float],
    quote_provider: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    event_day = event_dt.date()
    same = _at(bars, event_day)
    prior = _last_before(bars, event_day)
    next_bar = _first_after(bars, event_day)

    def quote_now() -> Dict[str, Any]:
        return _resolve_quote(symbol, quote, quote_provider)

    if session in ("premarket", "regular"):
        close_at = _close_dt(event_day)
        if computed_at < close_at:
            # 事件当天、收盘未到: 用当前报价做部分反应; 参照=事件前收盘
            q = quote_now()
            qprice = _quote_price(q, computed_at) if q else None
            qasof = _quote_asof(q)
            ref = _bar_num(prior, "close") if prior else None
            if qprice is not None and ref is not None:
                basis = "same_session_premarket" if session == "premarket" else "intraday"
                return _entry(
                    age_h=age_h, session=session, basis=basis,
                    pct=_pct(qprice, ref), reaction_at=computed_at,
                    reaction_price=qprice,
                    reference_at=_close_dt(_bar_date(prior)),
                    reference_price=ref, source="futu_quote",
                    quote_asof=qasof,
                )
            return _entry(
                age_h=age_h, session=session, reason="same_session_price_not_available",
                quote_asof=qasof,
            )
        if same is not None:
            reaction = _bar_num(same, "close")
            ref = _bar_num(prior, "close") if prior else None
            if reaction is not None and ref is not None:
                return _entry(
                    age_h=age_h, session=session, basis="same_day_close",
                    pct=_pct(reaction, ref), reaction_at=close_at,
                    reaction_price=reaction,
                    reference_at=_close_dt(_bar_date(prior)),
                    reference_price=ref, source="futu_daily_kline",
                )
        return _entry(
            age_h=age_h, session=session, reason="no_kline",
        )

    # 盘后/隔夜/休市: 参照=事件日收盘 (无当日 bar 则用之前最后收盘)
    ref_bar = same if same is not None else prior
    ref = _bar_num(ref_bar, "close") if ref_bar else None
    ref_at = _close_dt(_bar_date(ref_bar)) if ref_bar else None
    if ref is None:
        return _entry(
            age_h=age_h, session=session, reason="no_kline",
        )
    next_day = _next_trading_day(event_day, _calendar())
    next_pre_start = datetime.combine(next_day, time(4, 0), tzinfo=_ET)
    next_open = _open_dt(next_day)
    same_next_day = computed_at.date() == next_day
    if same_next_day and next_pre_start <= computed_at < next_open:
        q = quote_now()
        qprice = _quote_price(q, computed_at) if q else None
        if qprice is not None:
            return _entry(
                age_h=age_h, session=session, basis="next_premarket",
                pct=_pct(qprice, ref), reaction_at=computed_at,
                reaction_price=qprice,
                reference_at=ref_at, reference_price=ref,
                source="futu_quote", quote_asof=_quote_asof(q),
            )
    if next_bar is not None and computed_at >= next_open:
        reaction = _bar_num(next_bar, "open")
        if reaction is not None:
            return _entry(
                age_h=age_h, session=session, basis="next_open",
                pct=_pct(reaction, ref), reaction_at=_open_dt(_bar_date(next_bar)),
                reaction_price=reaction,
                reference_at=ref_at, reference_price=ref,
                source="futu_daily_kline",
            )
    if computed_at >= next_open:
        q = quote_now()
        qprice = _quote_price(q, computed_at) if q else None
        if qprice is not None:
            return _entry(
                age_h=age_h, session=session, basis="intraday",
                pct=_pct(qprice, ref), reaction_at=computed_at,
                reaction_price=qprice,
                reference_at=ref_at, reference_price=ref,
                source="futu_quote", quote_asof=_quote_asof(q),
            )
    return _entry(
        age_h=age_h, session=session, reason="premarket_not_started",
        reference_at=ref_at, reference_price=ref,
    )


def compute_event_reaction(
    *,
    event_time: Any,
    symbols: Iterable[Any],
    computed_at: datetime,
    bars_by_symbol: Dict[str, Sequence[Dict[str, Any]]],
    quotes_by_symbol: Optional[Dict[str, Dict[str, Any]]] = None,
    quote_provider: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
    max_symbols: int = 3,
) -> Dict[str, Any]:
    """构造 event_reaction_v1; 纯函数, 不触网/不写库."""
    computed_at = _as_et(computed_at) or datetime.now(_ET)
    event_dt = parse_event_time(event_time)
    syms = _symbols(symbols, max_symbols)
    payload: Dict[str, Any] = {
        "schema": EVENT_REACTION_SCHEMA,
        "computed_at": computed_at.isoformat(),
        "by_symbol": {},
    }
    if event_dt is None:
        payload["reason"] = "no_event_time"
        for symbol in syms:
            payload["by_symbol"][symbol] = _entry(
                age_h=None, session=None, reason="no_event_time")
        return payload
    if not syms:
        payload["reason"] = "no_symbol"
        return payload
    age_h = (computed_at - event_dt).total_seconds() / 3600.0
    if age_h < 0:
        payload["reason"] = "future_event"
        for symbol in syms:
            payload["by_symbol"][symbol] = _entry(
                age_h=round(age_h, 2), session=None, reason="future_event")
        return payload
    session = classify_event_session(event_dt)
    payload["event_session"] = session
    quotes = quotes_by_symbol or {}
    for symbol in syms:
        payload["by_symbol"][symbol] = _compute_one(
            symbol,
            event_dt=event_dt,
            session=session,
            computed_at=computed_at,
            bars=_bars_sorted(bars_by_symbol.get(symbol)),
            quote=quotes.get(symbol),
            age_h=round(age_h, 2),
            quote_provider=quote_provider,
        )
    return payload


_BASIS_LABELS = {
    "same_day_close": "同日收盘",
    "same_session_premarket": "同日盘前",
    "intraday": "盘中",
    "next_premarket": "次日盘前",
    "next_open": "次日开盘",
    "pending": "待确认",
    "unknown": "未知",
}


def _age_label(age_h: Optional[float]) -> str:
    if age_h is None:
        return "?"
    if age_h < 1:
        return f"{max(1, int(round(age_h * 60)))}m前"
    if age_h < 48:
        return f"{age_h:.1f}".rstrip("0").rstrip(".") + "h前"
    return f"{age_h / 24.0:.1f}".rstrip("0").rstrip(".") + "d前"


def render_event_reaction(
    reaction: Optional[Dict[str, Any]],
    symbols: Optional[Iterable[Any]] = None,
    max_symbols: int = 3,
) -> str:
    """紧凑渲染: "事件后: HIMS +3.77% (同日收盘, 23h前)"."""
    if not isinstance(reaction, dict):
        return ""
    by_symbol = reaction.get("by_symbol")
    if not isinstance(by_symbol, dict) or not by_symbol:
        return ""
    order = _symbols(symbols, max_symbols) if symbols else list(by_symbol)[:max_symbols]
    parts: List[str] = []
    for symbol in order:
        entry = by_symbol.get(symbol)
        if not isinstance(entry, dict):
            continue
        pct = entry.get("post_event_move_pct")
        basis = _BASIS_LABELS.get(str(entry.get("reaction_basis") or ""), "未知")
        age = _age_label(entry.get("age_h"))
        if pct is None:
            reason = str(entry.get("reason") or "unknown")
            parts.append(f"{symbol} 未知({reason})")
        else:
            parts.append(f"{symbol} {pct:+.2f}% ({basis}, {age})")
    return ("事件后: " + "; ".join(parts)) if parts else ""
