"""PULLBACK_LIMIT 承接失败判定（纯函数，无 IO、无下单）。

R5/R7 最小垂直切片：只使用 DA 已有 confirmation.price_level 和现有 K_1M。
1m bar 采用 Futu 分钟末标签：09:31–09:35 聚成 end=09:35 的完整 5m 窗口。
只有收齐 5 根 1m bar 的窗口才可判定；缺 bar、计划过期或数据陈旧一律 ABSTAIN。
R10/R11 修订：整窗排除 fill 所属窗口，且只取 fill 后头两根完整 5m，绝不滚动。

单位：价格=USD；pivot 与 fill 偏离=百分点；时间=ISO 字符串（naive 按 ET）。
known_at=本次实际取得数据并作出判断的 observed_at，绝不回填到 pivot/bar 时刻。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - 仅极端环境
    ET = timezone(timedelta(hours=-5))

ACTIVE = "ACTIVE"
STALE = "STALE"
FAILED_RECLAIM = "FAILED_RECLAIM"
WAIT = "WAIT"
ABSTAIN = "ABSTAIN"


def _price(value: Any) -> Optional[float]:
    try:
        if value is None or isinstance(value, bool):
            return None
        result = float(str(value).replace(",", ""))
        return result if result > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _dt(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=ET)
    return parsed.astimezone(ET)


def _bar_time(bar: Dict[str, Any]) -> Optional[datetime]:
    return _dt(bar.get("time") or bar.get("time_key"))


def _bar_key(value: datetime) -> str:
    # Futu naive time_key 的既有格式；有偏移的输入统一转 ET 后输出。
    return value.astimezone(ET).strftime("%Y-%m-%d %H:%M:00")


def _entry_window_end(entry: datetime) -> datetime:
    midnight = entry.replace(hour=0, minute=0, second=0, microsecond=0)
    minute = int((entry - midnight).total_seconds() // 60)
    return midnight + timedelta(minutes=((minute + 4) // 5) * 5)


def bars_after_fill_window(
    bars_1m: Iterable[Dict[str, Any]], *, entry_time: Any, observed_at: Any,
) -> List[Dict[str, Any]]:
    """返回 fill 所属 5m 窗口之后、本次实际可见的 1m bar（供前向路径）。"""
    entry = _dt(entry_time)
    now = _dt(observed_at)
    if entry is None or now is None:
        return []
    boundary = _entry_window_end(entry)
    rows = []
    for raw in bars_1m or []:
        if not isinstance(raw, dict):
            continue
        ts = _bar_time(raw)
        if ts is None or ts <= boundary or ts > now:
            continue
        values = {key: _price(raw.get(key)) for key in ("open", "high", "low", "close")}
        if any(value is None for value in values.values()):
            continue
        rows.append({"time": _bar_key(ts), **values})
    rows.sort(key=lambda bar: bar["time"])
    return rows


def aggregate_1m_to_5m(
    bars_1m: Iterable[Dict[str, Any]], *, observed_at: Any,
) -> List[Dict[str, Any]]:
    """聚合完整 5m 窗口；observed_at 之后的 bar 一律不可见。

    返回仅含 OHLC 均为正、且 5 个分钟末标签齐全的窗口。窗口 end 是最后一根
    1m bar 的分钟末标签；绝不把尚未收齐的窗口当成完整 bar。
    """
    now = _dt(observed_at)
    if now is None:
        return []
    grouped: Dict[Tuple[str, int], List[Tuple[datetime, Dict[str, Any]]]] = {}
    for raw in bars_1m or []:
        if not isinstance(raw, dict):
            continue
        ts = _bar_time(raw)
        if ts is None or ts > now:
            continue
        values = {k: _price(raw.get(k)) for k in ("open", "high", "low", "close")}
        if any(v is None for v in values.values()):
            continue
        midnight = ts.replace(hour=0, minute=0, second=0, microsecond=0)
        minute_of_day = int((ts - midnight).total_seconds() // 60)
        # 分钟末标签：31–35→35，36–40→40；00 视为前一窗口的整 5 分钟末。
        end_minute = ((minute_of_day + 4) // 5) * 5
        day = (midnight + timedelta(minutes=end_minute)).date().isoformat()
        grouped.setdefault((day, end_minute), []).append((ts, values))

    complete: List[Dict[str, Any]] = []
    for (_day, end_minute), members in grouped.items():
        unique = {ts: values for ts, values in members}
        if len(unique) != 5:
            continue
        ordered = sorted(unique.items(), key=lambda item: item[0])
        minutes = [int((ts - ts.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds() // 60)
                   for ts, _ in ordered]
        expected_end = minutes[-1]
        if end_minute - expected_end not in (0, 1440):
            continue
        if sorted(minutes) != list(range(expected_end - 4, expected_end + 1)):
            continue
        end_ts = ordered[-1][0]
        complete.append({
            "end": _bar_key(end_ts),
            "open": ordered[0][1]["open"],
            "high": max(values["high"] for _, values in ordered),
            "low": min(values["low"] for _, values in ordered),
            "close": ordered[-1][1]["close"],
            "n_1m": 5,
        })
    complete.sort(key=lambda bar: bar["end"])
    return complete


def evaluate_pullback(
    *, direction: str, pivot: Any, fill_price: Any, entry_time: Any,
    bars_1m: Iterable[Dict[str, Any]], observed_at: Any,
    max_plan_gap_pct: float = 1.0, max_bar_age_minutes: float = 15.0,
) -> Dict[str, Any]:
    """判断 PULLBACK_LIMIT 是否 FAILED_RECLAIM；不读取任何外部状态。"""
    now = _dt(observed_at)
    entry = _dt(entry_time)
    pivot_value = _price(pivot)
    fill = _price(fill_price)
    direction = str(direction or "").upper()
    is_short = direction in ("SHORT", "SELL_SHORT", "SELL")
    base = {
        "decision": ABSTAIN,
        "reason": "invalid_input",
        "plan_status": STALE,
        "known_at": now.isoformat() if now else None,
        "bar1": None,
        "bar2": None,
    }
    if now is None or entry is None or pivot_value is None or fill is None:
        return base
    if direction not in ("LONG", "BUY", "SHORT", "SELL_SHORT", "SELL"):
        return base | {"reason": "unsupported_direction"}
    if entry.date() != now.date():
        return base | {"reason": "cross_rth_day"}
    gap_pct = abs(fill / pivot_value - 1.0) * 100.0
    if gap_pct > max_plan_gap_pct:
        return base | {"reason": "plan_gap_stale", "plan_gap_pct": round(gap_pct, 6)}

    windows = aggregate_1m_to_5m(bars_1m, observed_at=now)
    # R10/R11：整窗排除 fill 所属窗口，而非只过滤 entry+1min 的窗口 end。
    # 例如 entry=09:36:10 ⇒ 所属窗口 end=09:40，09:36–09:40 全部不可作 bar1；
    # 随后仅取 fill 后头两根完整窗口，绝不滚动到数小时后的最近两窗。
    entry_window_end = _entry_window_end(entry)
    entry_window_end_key = _bar_key(entry_window_end)
    windows = [bar for bar in windows if bar["end"] > entry_window_end_key]
    if len(windows) < 2:
        return base | {"reason": "insufficient_complete_5m", "plan_status": ACTIVE,
                       "plan_gap_pct": round(gap_pct, 6)}
    bar1, bar2 = windows[0], windows[1]
    end1 = _dt(bar1["end"])
    end2 = _dt(bar2["end"])
    if end1 is None or end2 is None or (end2 - end1) != timedelta(minutes=5):
        return base | {"reason": "nonconsecutive_5m", "plan_status": ACTIVE,
                       "plan_gap_pct": round(gap_pct, 6), "bar1": bar1, "bar2": bar2}
    bar_age_minutes = (now - end2).total_seconds() / 60.0
    if bar_age_minutes < 0 or bar_age_minutes > max_bar_age_minutes:
        return base | {"reason": "stale_5m", "plan_status": ACTIVE,
                       "plan_gap_pct": round(gap_pct, 6), "bar1": bar1, "bar2": bar2,
                       "bar_age_minutes": round(bar_age_minutes, 3)}

    result = base | {
        "plan_status": ACTIVE,
        "plan_gap_pct": round(gap_pct, 6),
        "bar_age_minutes": round(bar_age_minutes, 3),
        "bar1": bar1,
        "bar2": bar2,
    }
    if is_short:
        failed = (
            bar1["close"] > pivot_value
            and bar2["close"] > pivot_value
            and bar2["low"] > pivot_value
            and bar2["high"] > bar1["high"]
        )
    else:
        failed = (
            bar1["close"] < pivot_value
            and bar2["close"] < pivot_value
            and bar2["high"] < pivot_value
            and bar2["low"] < bar1["low"]
        )
    return result | {"decision": FAILED_RECLAIM if failed else WAIT,
                     "reason": "reclaim_failed" if failed else "awaiting_structure"}


def decision_fingerprint(result: Dict[str, Any]) -> str:
    """给 state_hash 使用的稳定结构面，不含当前报价或决策结果。"""
    bar1, bar2 = result.get("bar1"), result.get("bar2")
    return "|".join([
        str(result.get("plan_status") or ""),
        str(result.get("plan_gap_pct") if result.get("plan_gap_pct") is not None else ""),
        str((bar1 or {}).get("end") or ""),
        str((bar2 or {}).get("end") or ""),
    ])
