from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional


MOM_ETF_V1_PREFIX = "MOM-ETF-V1-"
MOM_ETF_V1_ENTRIES_FLAG = "playbook.mom_etf_v1_entries_enabled"
logger = logging.getLogger(__name__)

# ── 纯本地美股日历 (2026-11-12 P0/P1 修复) ───────────────────────────────
# 为什么不用 get_holiday_calendar(): 它的 is_trading_day() 在缓存 miss 时会连
#   OpenD 拉 ~90 天日历。MOM 的 max_hold_deadline 在退出快循环里每 20s 调一次,
#   非交易路径抢 OpenD 槽位 (见 memory「Futu-Free Trading Day Source」)。
# 数据源: 2026 用 shadow_tracker.US_MARKET_HOLIDAYS_2026 (既定纯本地入口);
#   2027/2028 用 holiday_calendar.US_HOLIDAYS 的续编段 (2026-08-26 已核)。
#   ⚠️ 2026 刻意不取 US_HOLIDAYS: 该表把 2026-12-24 保守标为整休, 但 NYSE 当日
#   实际开市 (13:00 提前收盘) — 若并入会让 42 个交易日周期的下一次调仓晚 4 天。
_LOCAL_CALENDAR_YEARS = (2026, 2027, 2028)
_LOCAL_HOLIDAYS_CACHE: Optional[Dict[int, frozenset]] = None

# 21 交易日到期日缓存: 退出快循环每 20s 至少一次 (同一 entry_day 反复算)。
# 只含极少数键 (≤ 在途仓数 + 历史仓), LRU/FIFO 上界防无界增长; 失败兜底也缓存,
# 避免同一条坏 entry_day 每轮刷 ERROR (错误洪峰)。
_MAX_HOLD_DEADLINE_CACHE: "OrderedDict[tuple[str, str, int], date]" = OrderedDict()
_MAX_HOLD_DEADLINE_CACHE_MAX = 64
_INVALID_ENTRY_TIME_LOGGED: set = set()  # {(entry_time, entry_price, initial_sl, ET date)}


@dataclass(frozen=True)
class PilotPolicy:
    policy_id: str
    decision_prefix: str
    entry_mode: str
    exit_profile: str
    max_hold_trading_days: int
    take_profit_enabled: bool
    exit_allowlist: tuple[str, ...]
    initial_sl_pct: float
    overnight_gap_stop_pct: float
    weight_scheme: str
    instruction_validity: str
    rebalance_interval_trading_days: int
    cash_filter: str
    universe: tuple[str, ...]
    universe_version: str
    top_n: int
    lookback_days: int
    skip_days: int
    ma_filter_days: int
    anchor_date: str
    scheduled_et: str
    entry_cutoff_et: str
    max_open_positions: int
    max_leg_notional_usd: float
    max_daily_notional_usd: float
    max_fee_bps: float
    min_notional_usd: float


MOM_ETF_V1 = PilotPolicy(
    policy_id="mom_etf_v1",
    decision_prefix=MOM_ETF_V1_PREFIX,
    entry_mode="scheduled",
    exit_profile="swing_allowlist",
    max_hold_trading_days=21,
    take_profit_enabled=False,
    exit_allowlist=("SL_INITIAL", "OVERNIGHT_GAP_STOP", "MAX_HOLD"),
    initial_sl_pct=3.0,
    overnight_gap_stop_pct=5.0,
    weight_scheme="equal_notional",
    instruction_validity="same_day_only",
    rebalance_interval_trading_days=21,
    cash_filter="QQQ_CLOSE_ABOVE_MA200",
    universe=("US.QQQ", "US.XLE", "US.XLRE", "US.XLY"),
    universe_version="etf_v1_20261112",
    top_n=2,
    lookback_days=252,
    skip_days=21,
    ma_filter_days=200,
    anchor_date="2026-11-13",
    scheduled_et="09:50",
    entry_cutoff_et="14:00",
    max_open_positions=2,
    max_leg_notional_usd=4920.0,
    max_daily_notional_usd=9840.0,
    max_fee_bps=11.54,
    min_notional_usd=2500.0,
)


def policy_for(decision_id: str | None) -> PilotPolicy | None:
    text = str(decision_id or "")
    if text.startswith(MOM_ETF_V1_PREFIX):
        return MOM_ETF_V1
    return None


def is_mom_decision(decision_id: str | None) -> bool:
    return policy_for(decision_id) is not None


def mom_etf_v1_decision_id(trade_date: str, symbol: str) -> str:
    compact_date = str(trade_date).replace("-", "")
    return f"{MOM_ETF_V1_PREFIX}{compact_date}-{symbol}"


def entries_enabled() -> bool:
    try:
        from app.core.config_container import get_config
        return bool(get_config().get_dotted(MOM_ETF_V1_ENTRIES_FLAG, False))
    except Exception:
        return False


def _hhmm_to_minutes(text: str) -> int:
    hour, minute = text.split(":", 1)
    return int(hour) * 60 + int(minute)


def in_entry_window(now_et_dt: datetime, policy: PilotPolicy = MOM_ETF_V1) -> bool:
    minutes = now_et_dt.hour * 60 + now_et_dt.minute
    return _hhmm_to_minutes(policy.scheduled_et) <= minutes < _hhmm_to_minutes(policy.entry_cutoff_et)


def after_entry_cutoff(now_et_dt: datetime, policy: PilotPolicy = MOM_ETF_V1) -> bool:
    minutes = now_et_dt.hour * 60 + now_et_dt.minute
    return minutes >= _hhmm_to_minutes(policy.entry_cutoff_et)


@dataclass(frozen=True)
class RebalanceDayStatus:
    """调仓日判定的可观测结果 (纯函数产物, 不写日志/不发网络)。

    2026-11-12: 旧 `is_rebalance_day` 把日历故障吞成 False, 调用方无法区分
    「今天不是调仓日」与「日历坏了」。本结构把二者分开, 供调用方可选接入;
    `is_rebalance_day()` 保持原 bool API (故障时 fail-close + ERROR 留痕)。

    reason 取值:
      - before_anchor       今天早于策略 anchor 日
      - not_trading_day     今天不是交易日 (周末/假日)
      - rebalance_day       今天在周期上 (index % interval == 0)
      - off_cycle           今天是交易日但不在周期上
      - calendar_unavailable 本地日历不可用 (error 字段带异常, fail-close)
    """

    is_rebalance_day: bool
    reason: str
    trading_day_index: Optional[int] = None
    error: Optional[BaseException] = None

    def __bool__(self) -> bool:
        """显式布尔语义 (2026-11-12 核查加固)。

        frozen dataclass 恒 truthy —— 若调用方误写 `if rebalance_day_status(...)`,
        闸会**每天都开**(照常下 ETF 调仓单)且不报错。同一提交里的 `AMBIGUOUS` 哨兵
        已显式定义 `__bool__`, 本类型补齐, 让两种新类型防护对称。
        """
        return bool(self.is_rebalance_day)


def _load_local_holidays() -> Dict[int, frozenset]:
    """纯本地美股假日表 (按年, 不连 Futu OpenD)。

    2026 → shadow_tracker.US_MARKET_HOLIDAYS_2026 (既定本地入口);
    2027/2028 → holiday_calendar.US_HOLIDAYS 续编段 (只取常量, 不调用其日历单例)。
    覆盖年份外由 `_is_local_trading_day` 返回 None, 由调用方显式兜底。
    """
    global _LOCAL_HOLIDAYS_CACHE
    if _LOCAL_HOLIDAYS_CACHE is not None:
        return _LOCAL_HOLIDAYS_CACHE

    from app.playbook.shadow_tracker import US_MARKET_HOLIDAYS_2026
    from app.utils.holiday_calendar import US_HOLIDAYS

    by_year: Dict[int, set] = {year: set() for year in _LOCAL_CALENDAR_YEARS}
    for text in US_MARKET_HOLIDAYS_2026:
        d = date.fromisoformat(str(text))
        if d.year == 2026:
            by_year[2026].add(d)
    for d in US_HOLIDAYS:
        # 2026 见模块头注: US_HOLIDAYS 的 2026-12-24 保守标注与 NYSE 实际不符。
        if d.year in by_year and d.year != 2026:
            by_year[d.year].add(d)
    _LOCAL_HOLIDAYS_CACHE = {year: frozenset(days) for year, days in by_year.items()}
    return _LOCAL_HOLIDAYS_CACHE


def _is_local_trading_day(d: date) -> Optional[bool]:
    """纯本地交易日判定. None = 该年份超出本地日历覆盖 (不可判)."""
    holidays = _load_local_holidays().get(d.year)
    if holidays is None:
        return None
    return d.weekday() < 5 and d not in holidays


def _local_trading_days_in_range(start: date, end: date) -> List[date]:
    """纯本地 [start, end] 交易日列表 (含两端). 覆盖外年份 → ValueError."""
    days: List[date] = []
    d = start
    while d <= end:
        trading = _is_local_trading_day(d)
        if trading is None:
            raise ValueError(
                f"本地美股日历未覆盖 {d.isoformat()} (覆盖年份 {_LOCAL_CALENDAR_YEARS})")
        if trading:
            days.append(d)
        d += timedelta(days=1)
    return days


def previous_local_trading_day(today: date, max_days: int = 21) -> date:
    """返回纯本地上一交易日；覆盖外年份或异常窗口显式失败，不触 Futu。"""
    candidate = today - timedelta(days=1)
    for _ in range(max_days):
        trading = _is_local_trading_day(candidate)
        if trading is None:
            raise ValueError(f"本地美股日历未覆盖 {candidate.isoformat()}")
        if trading:
            return candidate
        candidate -= timedelta(days=1)
    raise ValueError(f"上一交易日超出 {max_days} 自然日搜索窗: {today.isoformat()}")


def rebalance_day_status(today: date, policy: PilotPolicy = MOM_ETF_V1) -> RebalanceDayStatus:
    """调仓日判定 (纯函数: 不用网络、不写日志)。故障时返回 calendar_unavailable。"""
    anchor = date.fromisoformat(policy.anchor_date)
    if today < anchor:
        return RebalanceDayStatus(False, "before_anchor")
    try:
        days = _local_trading_days_in_range(anchor, today)
    except Exception as exc:  # 本地日历不可用 → 如实上报, 由调用方决定留痕/降级
        return RebalanceDayStatus(False, "calendar_unavailable", error=exc)
    if not days or days[-1] != today:
        return RebalanceDayStatus(
            False, "not_trading_day", len(days) - 1 if days else None)
    index = len(days) - 1
    interval = max(int(policy.rebalance_interval_trading_days), 1)
    on_cycle = index % interval == 0
    return RebalanceDayStatus(
        on_cycle, "rebalance_day" if on_cycle else "off_cycle", index)


def is_rebalance_day(today: date, policy: PilotPolicy = MOM_ETF_V1) -> bool:
    """bool API (保持兼容): 故障时 fail-close 为 False, 但 ERROR 带栈可见。

    需要区分「非调仓日」与「日历故障」的调用方 (如 scripts/mom_etf_v1_generate.py)
    应改用 `rebalance_day_status()`, 读 reason/error 字段。
    """
    status = rebalance_day_status(today, policy)
    if status.error is not None:
        logger.error(
            "[MOM-ETF-V1] 调仓日历不可用, 按非调仓日 fail-close "
            "(today=%s, policy=%s, reason=%s): %s",
            today.isoformat(), policy.policy_id, status.reason, status.error,
            exc_info=status.error,
        )
    return status.is_rebalance_day


def _compute_max_hold_deadline(entry_day: date, policy: PilotPolicy) -> date:
    """纯本地计算第 N 个交易日 (entry_day 不计入)。覆盖外 → ValueError。"""
    if policy.max_hold_trading_days <= 0:
        return entry_day
    max_days = policy.max_hold_trading_days * 3 + 15
    current = entry_day
    counted = 0
    for _ in range(max_days):
        current += timedelta(days=1)
        trading = _is_local_trading_day(current)
        if trading is None:
            raise ValueError(
                f"本地美股日历未覆盖 {current.isoformat()} "
                f"(覆盖年份 {_LOCAL_CALENDAR_YEARS})")
        if trading:
            counted += 1
            if counted >= policy.max_hold_trading_days:
                return current
    raise ValueError(
        f"{max_days} 个自然日内未数满 {policy.max_hold_trading_days} 个交易日")


def max_hold_deadline(entry_day: date, policy: PilotPolicy = MOM_ETF_V1) -> Optional[date]:
    """21 交易日到期日 (纯本地日历 + 进程内有界缓存)。

    20s 退出快循环高频调用: 命中缓存零成本; 首次计算不连 Futu OpenD。
    失败 → 明确 ERROR (带栈) 并回落 entry_day+29 自然日 (约 20-21 交易日),
    且失败结果同样入缓存 ⇒ 同一条坏 entry_day 不会每 20s 刷一次错误。
    """
    key = (entry_day.isoformat(), policy.policy_id, policy.max_hold_trading_days)
    cached = _MAX_HOLD_DEADLINE_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        deadline = _compute_max_hold_deadline(entry_day, policy)
    except Exception as exc:
        deadline = entry_day + timedelta(days=29)
        logger.error(
            "[MOM-ETF-V1] %s 交易日到期日历不可用, 启用保守日历兜底 %s "
            "(entry=%s, policy=%s): %s",
            policy.max_hold_trading_days, deadline.isoformat(),
            entry_day.isoformat(), policy.policy_id, exc,
            exc_info=True,
        )
    _MAX_HOLD_DEADLINE_CACHE[key] = deadline
    while len(_MAX_HOLD_DEADLINE_CACHE) > _MAX_HOLD_DEADLINE_CACHE_MAX:
        _MAX_HOLD_DEADLINE_CACHE.popitem(last=False)
    return deadline


def _parse_entry_date(
    entry_time_str: str, now_et_dt: datetime,
) -> tuple[Optional[date], Optional[BaseException]]:
    """entry_time → (entry_day, error). 失败时 error 带可留痕的异常上下文。"""
    if not entry_time_str:
        return None, ValueError("entry_time 为空")
    try:
        entry_dt = datetime.fromisoformat(str(entry_time_str))
    except (TypeError, ValueError) as exc:
        return None, exc
    if entry_dt.tzinfo is not None and now_et_dt.tzinfo is not None:
        return entry_dt.astimezone(now_et_dt.tzinfo).date(), None
    if entry_dt.tzinfo is None:
        # 仓内 naive 时间约定为服务器 CST，不是 ET；跨日边界必须换算后再定 entry_day。
        from app.utils.timezone import to_et
        return to_et(entry_dt).date(), None
    return entry_dt.date(), None


def check_mom_exit(
    direction: str,
    entry_price: float,
    current_price: float,
    initial_sl: float,
    entry_time_str: str,
    now_et_dt: datetime,
    policy: PilotPolicy = MOM_ETF_V1,
) -> tuple[str, float]:
    if direction != "LONG" or entry_price <= 0 or current_price <= 0:
        return "", 0.0
    stop_loss = float(initial_sl or 0)
    if stop_loss <= 0:
        stop_loss = entry_price * (1.0 - policy.initial_sl_pct / 100.0)

    entry_day, entry_err = _parse_entry_date(entry_time_str, now_et_dt)
    if entry_day is None:
        # 2026-11-12 P0 修复: 旧实现 entry_time 缺失/非法直接伪造 MAX_HOLD → 立即错误平仓。
        #   时间型退出 (MAX_HOLD) 与依赖 entry_day 的 OVERNIGHT_GAP_STOP 缺证据 → 本轮
        #   不判定; 但价格型 SL_INITIAL 不依赖 entry_time, 必须继续保护。
        log_key = (entry_time_str, entry_price, initial_sl, now_et_dt.date())
        if log_key not in _INVALID_ENTRY_TIME_LOGGED:
            logger.error(
                "[MOM-ETF-V1] entry_time 缺失/非法 (%r), 无法判定时间型退出 — "
                "本轮不判 MAX_HOLD/OVERNIGHT_GAP_STOP, 价格型 SL_INITIAL 仍生效: %s",
                entry_time_str, entry_err, exc_info=entry_err,
            )
            _INVALID_ENTRY_TIME_LOGGED.add(log_key)
            if len(_INVALID_ENTRY_TIME_LOGGED) > 256:
                _INVALID_ENTRY_TIME_LOGGED.clear()
        if current_price <= stop_loss:
            return "SL_INITIAL", current_price
        return "", 0.0

    if entry_day < now_et_dt.date():
        profit_pct = (current_price - entry_price) / entry_price * 100.0
        if profit_pct <= -policy.overnight_gap_stop_pct:
            return "OVERNIGHT_GAP_STOP", current_price
    if current_price <= stop_loss:
        return "SL_INITIAL", current_price
    deadline = max_hold_deadline(entry_day, policy)
    if deadline is not None and now_et_dt.date() >= deadline:
        return "MAX_HOLD", current_price
    return "", 0.0


class MomDataIncompleteError(ValueError):
    """四 ETF 历史/价格不足，不得与 QQQ<MA200 的主动现金空仓混淆。"""


def compute_mom_candidates(
    bars_by_symbol: Dict[str, List[Any]],
    asof_date: str,
    policy: PilotPolicy = MOM_ETF_V1,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for symbol in policy.universe:
        bars = bars_by_symbol.get(symbol) or []
        if len(bars) < policy.lookback_days + 1:
            raise MomDataIncompleteError(
                f"{symbol}: bars={len(bars)} < required={policy.lookback_days + 1}")
        dates = [str(bar[0])[:10] for bar in bars]
        if dates[-1] != asof_date:
            raise MomDataIncompleteError(
                f"{symbol}: asof={dates[-1]} != expected={asof_date}")
        closes = [float(bar[4]) for bar in bars]
        if closes[-(policy.lookback_days + 1)] <= 0:
            raise MomDataIncompleteError(
                f"{symbol}: nonpositive close at T-{policy.lookback_days}")
        momentum = closes[-(policy.skip_days + 1)] / closes[-(policy.lookback_days + 1)] - 1.0
        ma_filter = sum(closes[-policy.ma_filter_days:]) / policy.ma_filter_days
        rows.append({
            "symbol": symbol,
            "close": closes[-1],
            "momentum_12_1": momentum,
            "ma_filter": ma_filter,
            "asof_date": asof_date,
        })
    qqq = next((row for row in rows if row["symbol"] == "US.QQQ"), None)
    if qqq is None or qqq["close"] <= qqq["ma_filter"]:
        return []
    rows.sort(key=lambda row: row["momentum_12_1"], reverse=True)
    return rows[:policy.top_n]


def size_mom_order(
    price: float,
    fee_estimator: Callable[[int, float], float],
    policy: PilotPolicy = MOM_ETF_V1,
) -> Optional[Dict[str, float]]:
    if price <= 0:
        return None
    quantity = int(policy.max_leg_notional_usd // price)
    if quantity <= 0:
        return None
    # 费用 bps 随名义额单调下降，最大合法股数就是唯一最优候选；
    # 再 +1 必越单腿帽，不做伪“搜索”。
    notional = quantity * price
    fee = float(fee_estimator(quantity, price))
    fee_bps = 10000.0 * fee / notional if notional > 0 else float("inf")
    if notional >= policy.min_notional_usd and fee_bps <= policy.max_fee_bps:
        return {
            "quantity": quantity,
            "notional": notional,
            "round_trip_fee": fee,
            "fee_bps": fee_bps,
        }
    return None


def mom_budget_reject_reason(
    notional: float,
    mom_open_count: int,
    mom_today_notional: float,
    global_today_notional: float,
    global_today_count: int,
    global_daily_notional_cap: float,
    global_max_daily_positions: int,
    global_deployed_notional: float = 0.0,
    global_budget_limit: float = 0.0,
    policy: PilotPolicy = MOM_ETF_V1,
) -> Optional[str]:
    if mom_open_count >= policy.max_open_positions:
        return "MOM_MAX_OPEN_POSITIONS"
    if notional > policy.max_leg_notional_usd:
        return "MOM_LEG_NOTIONAL_CAP"
    if mom_today_notional + notional > policy.max_daily_notional_usd:
        return "MOM_DAILY_NOTIONAL_CAP"
    if global_daily_notional_cap > 0 and global_today_notional + notional > global_daily_notional_cap:
        return "GLOBAL_DAILY_NOTIONAL_CAP"
    if global_max_daily_positions > 0 and global_today_count >= global_max_daily_positions:
        return "GLOBAL_MAX_DAILY_POSITIONS"
    if global_budget_limit > 0 and global_deployed_notional + notional > global_budget_limit:
        return "GLOBAL_BUYING_POWER_CAP"
    return None


def mom_reprice_params(
    inst: Dict[str, Any],
    ctx: Optional[Dict[str, Any]],
    trade_date: str,
    policy: PilotPolicy = MOM_ETF_V1,
    fee_estimator: Optional[Callable[[int, float], float]] = None,
) -> Optional[Dict[str, Any]]:
    quote = ctx or {}
    entry_price = float(
        quote.get("lv3_ask") or quote.get("ask")
        or quote.get("price") or quote.get("last") or 0
    )
    if entry_price <= 0:
        return None
    if fee_estimator is None:
        from app.execution.fee_model import estimate_round_trip

        def fee_estimator(quantity: int, price: float) -> float:
            return estimate_round_trip(quantity, price, price, "LONG", trade_date)["total"]

    sizing = size_mom_order(entry_price, fee_estimator, policy)
    if sizing is None:
        return None
    quantity = int(sizing["quantity"])
    stop_loss = entry_price * (1.0 - policy.initial_sl_pct / 100.0)
    return {
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "take_profit": 0.0,
        "position_size": quantity,
        "risk_amount": quantity * (entry_price - stop_loss),
        "reprice_from": float(inst.get("entry_price") or entry_price),
        "reprice_to": entry_price,
        "reprice_original_sl": float(inst.get("stop_loss") or stop_loss),
    }


def mom_final_order_reject_reason(
    entry_price: float,
    quantity: int,
    trade_date: str,
    mom_today_notional: float = 0.0,
    policy: PilotPolicy = MOM_ETF_V1,
    fee_estimator: Optional[Callable[[int, float], float]] = None,
) -> Optional[str]:
    if entry_price <= 0 or quantity <= 0:
        return "MOM_FINAL_SIZE_INVALID"
    notional = entry_price * quantity
    if notional > policy.max_leg_notional_usd:
        return "MOM_LEG_NOTIONAL_CAP"
    if notional < policy.min_notional_usd:
        return "MOM_FINAL_NOTIONAL_TOO_SMALL"
    if mom_today_notional + notional > policy.max_daily_notional_usd:
        return "MOM_DAILY_NOTIONAL_CAP"
    if fee_estimator is None:
        from app.execution.fee_model import estimate_round_trip

        def fee_estimator(final_quantity: int, price: float) -> float:
            return estimate_round_trip(final_quantity, price, price, "LONG", trade_date)["total"]

    fee = float(fee_estimator(quantity, entry_price))
    fee_bps = 10000.0 * fee / notional
    if fee_bps > policy.max_fee_bps:
        return "MOM_FINAL_FEE_TOO_HIGH"
    return None


def mom_budget_snapshot(
    now_et_dt: datetime,
    policy: PilotPolicy = MOM_ETF_V1,
) -> tuple[int, float]:
    today = now_et_dt.date().isoformat()
    from app.core.db_utils import safe_sqlite_connect
    from app.core.paths import get_trade_records_db_for_arm

    connection = safe_sqlite_connect(get_trade_records_db_for_arm("sim"), timeout=5)
    try:
        open_count = int(connection.execute(
            "SELECT COUNT(*) FROM trade_records WHERE decision_id LIKE ? "
            "AND (exit_time IS NULL OR exit_time='')",
            (MOM_ETF_V1_PREFIX + "%",),
        ).fetchone()[0])
        today_notional = float(connection.execute(
            "SELECT COALESCE(SUM(entry_price * quantity), 0) FROM trade_records "
            "WHERE decision_id LIKE ? AND substr(entry_time, 1, 10)=?",
            (MOM_ETF_V1_PREFIX + "%", today),
        ).fetchone()[0])
    finally:
        connection.close()
    return open_count, today_notional
