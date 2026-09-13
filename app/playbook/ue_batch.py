"""Small UE batch integrity and fixed-endpoint measurement helpers (no network IO)."""
from contextlib import contextmanager
from datetime import date, datetime, time
import fcntl
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo

SCORE_VERSION = "ue_t1_close_v1"


def latest_valid_batch(records, today, now, max_age_hours, window="premarket"):
    """取当日指定窗口的最新**有效**批次; 更晚开始/失败/空的尝试使更早成功批次失效。

    口径 (P1.5):
      - 只看同一 scan_window 的尝试 (premarket / da_feed / midday 各走原定用途,
        不得互相顶替);
      - 批次的最后一条记录必须是 type=="scan" 且 batch_status=="complete";
      - 时间戳缺失/无时区/未来/超过 max_age_hours → 一律拒绝 (不静默回用旧候选);
      - 空批次 (surfaced=[]) 是"当日无机会"的正常结束, 由调用方按空处理, 不是失败。

    Returns:
        (record|None, reason) — reason ∈ complete / no_{window}_attempt /
        stale_or_future / missing_or_invalid_timestamp / <batch_status>
    """
    attempts = [r for r in records if r.get("date") == today
                and r.get("scan_window", "premarket") == window
                and r.get("type") in ("batch_start", "scan", "batch_end")]
    if not attempts:
        return None, f"no_{window}_attempt"
    last = attempts[-1]
    # Completion order is not start order when two scans overlap.
    starts = [r for r in attempts if r.get("type") == "batch_start"]
    if starts:
        bid = starts[-1].get("batch_id")
        matching = [r for r in attempts if r.get("batch_id") == bid]
        last = matching[-1]
    if last.get("type") != "scan" or last.get("batch_status", "complete") != "complete":
        return None, last.get("batch_status", "pending")
    stamp = last.get("started_at") or last.get("scanned_at")
    try:
        dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        if dt.tzinfo is None or now.tzinfo is None:
            raise ValueError("timezone required")
        age = (now - dt).total_seconds() / 3600
        if not 0 <= age <= float(max_age_hours):
            return None, "stale_or_future"
    except (TypeError, ValueError):
        return None, "missing_or_invalid_timestamp"
    return last, "complete"


def latest_premarket(records, today, now, max_age_hours):
    """UE inject 专用: 只允许消费最新有效 premarket 批次。"""
    return latest_valid_batch(records, today, now, max_age_hours, window="premarket")


@contextmanager
def injection_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def raw_instruction(row):
    raw = row.get("raw_instruction") or row.get("raw_instruction_json") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return {}
    return raw if isinstance(raw, dict) else {}


def effective_ue_symbols(rows, source):
    # Count a unique symbol once even when terminal executions have multiple rounds.
    return {r.get("symbol") for r in rows
            if str(r.get("direction", "")).upper() in ("BUY", "SELL_SHORT", "LONG", "SHORT")
            and (str(r.get("decision_id", "")).startswith(source + ":")
                 or raw_instruction(r).get("source") == source)
            and not r.get("skip_reason")}


def confirm_saved(db, instruction, returned_id):
    """Return saved/rejected/unknown; returned IDs alone are not admission proof."""
    if returned_id is None:
        return "unknown"
    try:
        row = db.get_instruction_by_id(returned_id)
    except Exception:
        return "unknown"
    if not isinstance(row, dict) or not row:
        return "unknown"
    wanted = raw_instruction(instruction)
    actual = raw_instruction(row)
    cid = (wanted.get("candidate_screening") or {}).get("candidate_id")
    if not cid:
        # 无候选身份 ⇒ 无法核对是不是本轮那行 (初筛整体 fail-open 时会走到这里)。
        #   这是"结果不明", 不是"确定拒绝" —— 计划纪律: 确定拒绝才补下一名,
        #   结果不明先核对, 禁止盲目重写或无限补位。
        return "unknown"
    # SKIP 行的 skip_reason 是**预期内容** (资格拒绝的审计行本身就要落库),
    #   只有它被改写才算拒绝; 合格指令 (BUY/SELL_SHORT) 落成 SKIP 才是确定拒绝
    #   (admission 降级等)。修前一律按 truthy 判拒绝 ⇒ 离线全链实测 (2026-09-12)
    #   把成功写入的 id=1/2 两行误报成"写库仲裁拒绝 → 补位", 归因与计数双错。
    intended_skip = str(instruction.get("direction") or "").upper() == "SKIP"
    wanted_skip = str(instruction.get("skip_reason") or "")
    actual_skip = str(row.get("skip_reason") or "")
    skip_mismatch = (actual_skip != wanted_skip) if intended_skip else bool(actual_skip)
    if ((actual.get("candidate_screening") or {}).get("candidate_id") != cid
            or row.get("decision_id") != instruction.get("decision_id")
            or row.get("symbol") != instruction.get("symbol")
            or row.get("direction") != instruction.get("direction")
            or actual.get("source") != wanted.get("source")
            or skip_mismatch):
        return "rejected"
    if str(row.get("status", "")).upper() in ("REJECTED", "SKIP", "CANCELLED"):
        return "rejected"
    if row.get("executed") or row.get("outcome") not in (None, "", "PENDING"):
        return "rejected"
    return "saved"


def positive(value):
    # bool 是 int 子类 ⇒ 显式排除 (与 candidate_eligibility.positive_number 同口径):
    #   True 当价格 1.0 会让"缺价补算"用 1 美元当真值算前瞻收益。
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) and value > 0 else None
    except (ValueError, TypeError):
        return None


_US_MARKET = "US"
_US_REGULAR_CLOSE_MINUTES = 16 * 60      # 与 holiday_calendar 同值 (读不到时兜底)


def _default_calendar():
    """仓内交易日历 SSOT (Futu 权威 + 离线假日表降级)。

    不用第三方 exchange_calendars: 本环境未安装 ⇒ 那条路径恒抛 ImportError,
    被 candidate_score 的 except 吞成 "calendar_unavailable" — 固定终点评分
    会静默永不产出 (AGENT.md "静默吞 = 门死了没人知道" 的同类死门)。
    """
    from app.utils.holiday_calendar import get_holiday_calendar
    return get_holiday_calendar()


def _us_close_minutes(day):
    """当日收盘时刻 (ET 分钟数): 提前收盘日 780 (13:00), 常规 960 (16:00)。"""
    try:
        from app.utils.holiday_calendar import get_us_close_minutes_et
        return int(get_us_close_minutes_et(day))
    except Exception:
        return _US_REGULAR_CLOSE_MINUTES


def next_close(scan_date, calendar=None):
    """固定的**下一交易日正式收盘**时刻 (ET, tz-aware) — 评价终点单一口径。

    为什么不用"评分命令运行时的现价": cmd_score 旧口径在任意时刻取实时报价,
    同一批次今天跑和下周跑得到不同"前瞻收益" ⇒ 尺子本身会漂 (计划 P4.1)。
    固定终点 = 扫描日的下一交易日收盘, 与运行时刻无关, 且可补算/幂等。

    Args:
        scan_date: "YYYY-MM-DD" / date / **tz-aware** datetime (naive 直接拒绝:
            app.utils.timezone.to_et() 把 naive 当北京时间, 静默转换会移半天,
            跨日边界会把终点算错一天)。
        calendar: 可注入的日历 (需实现 `next_trading_day(market, from_date)`),
            缺省走仓内 SSOT。注入是为了让 DST/假日/补算用例可离线断言。

    Returns:
        tz-aware datetime — 按 ET 墙上时间构造 (16:00 或提前收盘日 13:00),
        跨 DST 由 America/New_York 时区自身处理, 不做固定 UTC 偏移。
    """
    et = ZoneInfo("America/New_York")
    if isinstance(scan_date, datetime):
        if scan_date.tzinfo is None:
            raise ValueError("scan_date datetime must be timezone-aware")
        scan_day = scan_date.astimezone(et).date()
    elif isinstance(scan_date, date):
        scan_day = scan_date
    elif isinstance(scan_date, str):
        scan_day = date.fromisoformat(scan_date.strip()[:10])
    else:
        raise TypeError(f"unsupported scan_date type: {type(scan_date).__name__}")
    calendar = calendar if calendar is not None else _default_calendar()
    target = calendar.next_trading_day(_US_MARKET, scan_day)
    if target is None:
        raise LookupError(f"next trading day unavailable after {scan_day}")
    if isinstance(target, datetime):
        target = target.date()
    minutes = _us_close_minutes(target)
    return datetime.combine(target,
                            time(hour=minutes // 60, minute=minutes % 60),
                            tzinfo=et)


def candidate_score(scan, candidate, now, bars, calendar=None):
    """No quotes: only the exact completed T+1 session bar is an endpoint."""
    screening = candidate.get("candidate_screening") or {}
    cid = screening.get("candidate_id") or candidate.get("candidate_id")
    result = {"type": "score_candidate", "score_version": SCORE_VERSION,
              "batch_id": scan.get("batch_id"), "candidate_id": cid,
              "scan_date": scan.get("date"), "symbol": candidate.get("symbol"),
              "direction": candidate.get("direction"), "scored_at": now.isoformat(),
              "status": "pending", "policy_version": scan.get("policy_version"),
              "self_tier": candidate.get("self_tier"),
              "emergence_streak_days": candidate.get("emergence_streak_days")}
    if not cid:
        result.update(status="censored", reason="missing_candidate_identity")
        return result
    try:
        endpoint = next_close(scan["date"], calendar)
        result["endpoint_at"] = endpoint.isoformat()
    except Exception as exc:
        result["reason"] = "calendar_unavailable:" + type(exc).__name__
        return result
    if now < endpoint:
        result["reason"] = "endpoint_not_complete"
        return result
    target_date = endpoint.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    matches = [b for b in bars or []
               if str(b.get("time") or b.get("date") or "")[:10] == target_date
               and b.get("complete", True) is not False]
    prices = {positive(b.get("close")) for b in matches}
    prices.discard(None)
    base = positive(candidate.get("scan_price"))
    if len(prices) != 1 or base is None:
        result["reason"] = "missing_or_ambiguous_price"
        return result
    price = prices.pop()
    result.update(status="complete", scan_price=base, scored_price=price,
                  raw_fwd_pct=(price - base) / base * 100,
                  measurement="price_return_not_net_execution_pnl")
    # A context-only/pre-LLM row must never acquire a model hit rate.
    if candidate.get("direction") in ("LONG", "SHORT") and candidate.get("llm_evaluated", True):
        fwd = result["raw_fwd_pct"] * (1 if candidate["direction"] == "LONG" else -1)
        result.update(fwd_pct=round(fwd, 4), hit=fwd >= 1.0)
    else:
        result["direction_hint"] = candidate.get("direction_hint")
    return result
