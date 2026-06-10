"""
Pre-Trade Safety Gate — SSOT for all position limit checks.

根因修复 (2026-06-06): 替代散落在 order_executor.py (3处) 和 orb/engine.py (2处) 的
inline 仓位校验逻辑。所有路径统一调用此函数，消除遗漏和漂移。

三层检查:
  1. 单品种名义 ≤ equity × max_per_symbol (default 0.25)
  2. 同方向名义 ≤ equity × max_per_direction (default 0.65)
  3. 总敞口名义 ≤ equity × max_total (default 0.80)

调用点:
  - _get_order_qty (真实股数) → 截断 quantity
  - _submit_order (预估算) → 拒绝入场
  - orb._submit_order (真实股数) → 拒绝下单
"""

import logging
import time
from typing import Tuple, Optional, Any

logger = logging.getLogger(__name__)

# ── Default limits (overridden by unified_config.yaml) ──
_DEFAULTS = {
    "max_per_symbol": 0.25,
    "max_per_direction": 0.65,
    "max_total": 0.80,
}

# ── P0 (2026-06-07): Daily realized loss circuit breaker ──
# Cache: (timestamp, daily_pnl) — 60s TTL to avoid DB query on every order
_daily_pnl_cache = {"ts": 0, "pnl": 0.0, "date": ""}
_DAILY_PNL_TTL = 60  # seconds


def _load_limits(config=None) -> dict:
    """Load position limits from config, falling back to defaults.

    TradingMode-aware (2026-06-10): limits scale with risk_multiplier.
    LIVE_CONSERVATIVE (0.5x) → all limits halved.
    Also loads long_max_per_direction for directional bias.
    """
    limits = dict(_DEFAULTS)
    try:
        if config is None:
            from app.core.config_container import get_config
            config = get_config()
        limits["max_per_symbol"] = float(
            config.get_dotted("risk.position.max_per_symbol", _DEFAULTS["max_per_symbol"])
        )
        limits["max_per_direction"] = float(
            config.get_dotted("risk.position.max_per_direction", _DEFAULTS["max_per_direction"])
        )
        limits["max_total"] = float(
            config.get_dotted("risk.position.max_total", _DEFAULTS["max_total"])
        )
        # LONG direction cap (2026-06-10): restrict LONG when data shows SHORT outperforms
        limits["long_max_per_direction"] = float(
            config.get_dotted("risk.position.long_max_per_direction", limits["max_per_direction"])
        )
    except Exception:
        pass

    # Scale limits by TradingMode risk_multiplier (2026-06-10)
    try:
        from app.core.trading_mode import TradingMode
        tm = TradingMode()
        mult = tm.get_risk_multiplier()
        if mult < 1.0:
            limits["max_per_symbol"] *= mult
            limits["max_per_direction"] *= mult
            limits["max_total"] *= mult
            limits["long_max_per_direction"] *= mult

        # Mode-adaptation caps from YAML (2026-06-10 fix): 取 YAML 阈值与缩放后值的较小者
        mode_max_pos = tm.get_max_position_pct()
        mode_max_exp = tm.get_max_total_exposure_pct()
        if mode_max_pos > 0:
            limits["max_per_symbol"] = min(limits["max_per_symbol"], mode_max_pos)
        if mode_max_exp > 0:
            limits["max_total"] = min(limits["max_total"], mode_max_exp)
            limits["max_per_direction"] = min(limits["max_per_direction"], mode_max_exp)
            limits["long_max_per_direction"] = min(limits["long_max_per_direction"], mode_max_exp)
    except Exception:
        pass

    return limits


def _get_equity() -> float:
    """Get current account equity.

    实盘 fail-close (2026-06-10): equity 不可用时返回 0 → 拒绝所有交易
    模拟盘: 保持 fallback $100K (容错开发)
    """
    try:
        from app.risk.capital_safety import get_capital_safety_system
        cap = get_capital_safety_system()
        eq = cap._state.current_equity
        if eq > 0:
            return eq
    except Exception:
        pass

    # 实盘模式: equity 不可用 → fail-close
    try:
        from app.core.trading_mode import TradingMode
        if TradingMode().is_live:
            import logging
            logging.getLogger(__name__).critical(
                "🚫 [pre_trade_gate] 实盘模式但 equity 不可用 — 拒绝所有交易 (fail-close)"
            )
            return 0.0
    except Exception:
        pass

    return 100_000.0  # 模拟盘 fallback


def _is_short(direction: str) -> bool:
    """Normalize direction to boolean."""
    d = direction.upper() if direction else ""
    if hasattr(direction, 'value'):
        d = direction.value.upper()
    return "SHORT" in d or "SELL" in d


def _check_daily_drawdown(equity: float) -> Tuple[bool, str]:
    """P0 (2026-06-07): Check if daily realized loss exceeds circuit breaker.

    Queries trade_records for today's realized PnL.
    Uses 60s TTL cache to avoid DB query on every order.

    Returns:
        (allowed, reason) — False if daily loss exceeds threshold.
    """
    global _daily_pnl_cache

    # Load threshold from config (default 3%)
    max_daily_dd = 0.03
    try:
        from app.core.config_container import get_config
        cfg = get_config()
        max_daily_dd = float(cfg.get_dotted(
            "risk.account.max_daily_drawdown", 0.03))
    except Exception:
        pass

    if max_daily_dd <= 0:
        return True, ""

    # Check cache validity
    now = time.time()
    # Use ET date for "today"
    from datetime import datetime, timezone, timedelta
    _ET = timezone(timedelta(hours=-4))
    today = datetime.now(_ET).strftime('%Y-%m-%d')

    if (_daily_pnl_cache["date"] == today
            and now - _daily_pnl_cache["ts"] < _DAILY_PNL_TTL):
        daily_pnl = _daily_pnl_cache["pnl"]
    else:
        # Query DB
        daily_pnl = 0.0
        try:
            import sqlite3
            import os
            from app.core.db_utils import get_main_db
            db_path = str(get_main_db())
            if os.path.exists(db_path):
                conn = sqlite3.connect(db_path)
                conn.execute("PRAGMA journal_mode=WAL")
                c = conn.cursor()
                c.execute(
                    "SELECT COALESCE(SUM(realized_pnl), 0) "
                    "FROM trade_records "
                    "WHERE exit_time IS NOT NULL "
                    "AND date(exit_time) = ?",
                    (today,)
                )
                row = c.fetchone()
                if row:
                    daily_pnl = float(row[0])
                conn.close()
        except Exception as e:
            logger.debug(f"[pre_trade_gate] daily PnL query failed: {e}")
            return True, ""  # fail-open on DB error (don't block trading)

        _daily_pnl_cache = {"ts": now, "pnl": daily_pnl, "date": today}

    # Check threshold
    if equity > 0 and daily_pnl < 0:
        dd_pct = abs(daily_pnl) / equity
        if dd_pct >= max_daily_dd:
            reason = (
                f"🚫 日亏损熔断: 当日已实现亏损 ${abs(daily_pnl):,.0f} "
                f"({dd_pct:.1%}) ≥ 阈值 {max_daily_dd:.0%}"
            )
            logger.critical(f"[pre_trade_gate] {reason}")
            return False, reason
        elif dd_pct >= max_daily_dd * 0.7:
            # Warning at 70% of threshold
            logger.warning(
                f"[pre_trade_gate] ⚠️ 日亏损预警: ${abs(daily_pnl):,.0f} "
                f"({dd_pct:.1%}) 接近熔断阈值 {max_daily_dd:.0%}"
            )

    return True, ""


def _sum_direction_notional(position_store, short: bool) -> float:
    """Sum notional of all positions in the given direction."""
    total = 0.0
    try:
        for pos in position_store.all():
            pos_dir = pos.direction.value if hasattr(pos.direction, 'value') else str(pos.direction or "")
            if ("SHORT" in pos_dir.upper()) == short:
                total += pos.size * pos.avg_entry_price
    except Exception:
        pass
    return total


def _sum_total_notional(position_store) -> float:
    """Sum notional of ALL positions regardless of direction."""
    total = 0.0
    try:
        for pos in position_store.all():
            total += pos.size * pos.avg_entry_price
    except Exception:
        pass
    return total


def pre_trade_safety_check(
    symbol: str,
    direction: str,
    quantity: int,
    price: float,
    position_store: Optional[Any] = None,
    equity: Optional[float] = None,
    config: Optional[Any] = None,
) -> Tuple[bool, int, str]:
    """
    Unified pre-trade safety gate. SSOT for all position limit checks.

    Args:
        symbol: e.g. "US.XLF"
        direction: "LONG" or "SHORT" (or enum with .value)
        quantity: planned order quantity (shares)
        price: estimated/current price
        position_store: object with .get(symbol) and .all() methods
        equity: account equity (loaded from capital_safety if None)
        config: config container (loaded from get_config if None)

    Returns:
        (allowed, max_quantity, reason)
        - allowed: True if order passes all three checks
        - max_quantity: maximum allowed qty (may be < requested)
        - reason: human-readable explanation if rejected, "" if allowed
    """
    if quantity <= 0 or price <= 0:
        return True, quantity, ""

    if equity is None:
        equity = _get_equity()

    limits = _load_limits(config)
    plan_notional = float(quantity) * price

    # ── Existing position for this symbol ──
    existing_symbol_notional = 0.0
    if position_store is not None:
        try:
            ep = position_store.get(symbol)
            if ep:
                existing_symbol_notional = ep.size * ep.avg_entry_price
        except Exception:
            pass

    max_qty = quantity
    reject_reason = ""
    short = _is_short(direction)
    dir_label = "SHORT" if short else "LONG"

    # ═══ Check 0: Daily realized loss circuit breaker (P0 2026-06-07) ═══
    dd_ok, dd_reason = _check_daily_drawdown(equity)
    if not dd_ok:
        return False, 0, dd_reason

    # ═══ Check 1: Per-symbol nominal cap ═══
    symbol_limit = equity * limits["max_per_symbol"]
    symbol_total = existing_symbol_notional + plan_notional
    if symbol_total > symbol_limit:
        max_from_symbol = max(0, int((symbol_limit - existing_symbol_notional) / price))
        if max_from_symbol < quantity:
            max_qty = min(max_qty, max(1, max_from_symbol))
            reject_reason = (
                f"🚫 [单品种上限] {symbol} {dir_label}: "
                f"已有=${existing_symbol_notional:,.0f} + 计划=${plan_notional:,.0f} = "
                f"${symbol_total:,.0f} > 上限=${symbol_limit:,.0f} "
                f"(净值${equity:,.0f}×{limits['max_per_symbol']:.0%})"
            )
            if max_from_symbol <= 0:
                return False, 0, reject_reason

    # ═══ Check 2: Per-direction exposure cap ═══
    # LONG direction uses separate cap (2026-06-10): data shows SHORT 10/10 WR vs LONG 1/4
    effective_dir_limit = limits["max_per_direction"]
    if not short and "long_max_per_direction" in limits:
        effective_dir_limit = min(effective_dir_limit, limits["long_max_per_direction"])
    direction_limit = equity * effective_dir_limit
    direction_existing = _sum_direction_notional(position_store, short) if position_store else 0.0
    direction_total = direction_existing + plan_notional
    if direction_total > direction_limit:
        max_from_dir = max(0, int((direction_limit - direction_existing) / price))
        if max_from_dir < quantity:
            max_qty = min(max_qty, max(1, max_from_dir))
            if not reject_reason:
                reject_reason = (
                    f"🚫 [方向敞口上限] {symbol} {dir_label}: "
                    f"同方向已有=${direction_existing:,.0f} + 计划=${plan_notional:,.0f} = "
                    f"${direction_total:,.0f} > 上限=${direction_limit:,.0f} "
                    f"(净值${equity:,.0f}×{limits['max_per_direction']:.0%})"
                )
            if max_from_dir <= 0:
                return False, 0, reject_reason

    # ═══ Check 3: Total exposure cap ═══
    total_limit = equity * limits["max_total"]
    total_existing = _sum_total_notional(position_store) if position_store else 0.0
    total_new = total_existing + plan_notional
    if total_new > total_limit:
        max_from_total = max(0, int((total_limit - total_existing) / price))
        if max_from_total < quantity:
            max_qty = min(max_qty, max(1, max_from_total))
            if not reject_reason:
                reject_reason = (
                    f"🚫 [总敞口上限] {symbol} {dir_label}: "
                    f"已有=${total_existing:,.0f} + 计划=${plan_notional:,.0f} = "
                    f"${total_new:,.0f} > 上限=${total_limit:,.0f} "
                    f"(净值${equity:,.0f}×{limits['max_total']:.0%})"
                )
            if max_from_total <= 0:
                return False, 0, reject_reason

    # ── Final verdict ──
    if max_qty < quantity:
        # Truncation needed — caller decides whether to cap or reject
        return False, max_qty, reject_reason

    return True, quantity, ""
