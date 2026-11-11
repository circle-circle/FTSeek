from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional


MOM_ETF_V1_PREFIX = "MOM-ETF-V1-"
MOM_ETF_V1_ENTRIES_FLAG = "playbook.mom_etf_v1_entries_enabled"
logger = logging.getLogger(__name__)


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


def is_rebalance_day(today: date, policy: PilotPolicy = MOM_ETF_V1) -> bool:
    anchor = date.fromisoformat(policy.anchor_date)
    if today < anchor:
        return False
    try:
        from app.utils.holiday_calendar import get_holiday_calendar
        days = get_holiday_calendar().trading_days_in_range("US", anchor, today)
    except Exception as exc:
        logger.warning("[MOM-ETF-V1] 调仓日历不可用, 按非调仓日 fail-close: %s", exc)
        return False
    if not days or days[-1] != today:
        return False
    return (len(days) - 1) % policy.rebalance_interval_trading_days == 0


def max_hold_deadline(entry_day: date, policy: PilotPolicy = MOM_ETF_V1) -> Optional[date]:
    try:
        from app.utils.holiday_calendar import get_holiday_calendar
        deadline = get_holiday_calendar().nth_trading_day_after(
            "US", entry_day, policy.max_hold_trading_days,
            max_days=policy.max_hold_trading_days * 3 + 15,
        )
        if deadline is not None:
            return deadline
        raise ValueError("holiday calendar returned no deadline")
    except Exception as exc:
        fallback = entry_day + timedelta(days=29)
        logger.error(
            "[MOM-ETF-V1] 21 交易日到期日历不可用, 启用保守日历兜底 %s "
            "(entry=%s, policy=%s): %s",
            fallback.isoformat(), entry_day.isoformat(), policy.policy_id, exc,
        )
        return fallback


def _entry_date(entry_time_str: str, now_et_dt: datetime) -> Optional[date]:
    if not entry_time_str:
        return None
    try:
        entry_dt = datetime.fromisoformat(entry_time_str)
        if entry_dt.tzinfo is not None and now_et_dt.tzinfo is not None:
            return entry_dt.astimezone(now_et_dt.tzinfo).date()
        return entry_dt.date()
    except Exception:
        return None


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
    entry_day = _entry_date(entry_time_str, now_et_dt)
    if entry_day is None:
        logger.error(
            "[MOM-ETF-V1] entry_time 缺失/非法, 无法计算 21 交易日上限, "
            "保守按 MAX_HOLD 退出 (entry_time=%r)", entry_time_str,
        )
        return "MAX_HOLD", current_price
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


def compute_mom_candidates(
    bars_by_symbol: Dict[str, List[Any]],
    asof_date: str,
    policy: PilotPolicy = MOM_ETF_V1,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for symbol in policy.universe:
        bars = bars_by_symbol.get(symbol) or []
        if len(bars) < policy.lookback_days + 1:
            return []
        dates = [str(bar[0])[:10] for bar in bars]
        if dates[-1] != asof_date:
            return []
        closes = [float(bar[4]) for bar in bars]
        if closes[-(policy.lookback_days + 1)] <= 0:
            return []
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
    while quantity * price <= policy.max_leg_notional_usd:
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
        quantity += 1
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
