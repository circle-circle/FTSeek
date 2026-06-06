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
from typing import Tuple, Optional, Any

logger = logging.getLogger(__name__)

# ── Default limits (overridden by unified_config.yaml) ──
_DEFAULTS = {
    "max_per_symbol": 0.25,
    "max_per_direction": 0.65,
    "max_total": 0.80,
}


def _load_limits(config=None) -> dict:
    """Load position limits from config, falling back to defaults."""
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
    except Exception:
        pass
    return limits


def _get_equity() -> float:
    """Get current account equity."""
    try:
        from app.risk.capital_safety import get_capital_safety_system
        cap = get_capital_safety_system()
        eq = cap._state.current_equity
        if eq > 0:
            return eq
    except Exception:
        pass
    return 100_000.0  # fallback


def _is_short(direction: str) -> bool:
    """Normalize direction to boolean."""
    d = direction.upper() if direction else ""
    if hasattr(direction, 'value'):
        d = direction.value.upper()
    return "SHORT" in d or "SELL" in d


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
    direction_limit = equity * limits["max_per_direction"]
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
