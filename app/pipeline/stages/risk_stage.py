"""
Risk Stage — 仓位计算 + OrderRequest 构建

职责:
  1. 仓位计算 (PositionSizer — 资金水位 + tier + 风险参数)
  2. 止损/止盈计算 (ATR-based)
  3. 构建 OrderRequest (已审批, 可直接提交执行引擎)

注意: 风控门禁 (PreTradeChecker/TradeGuard/VIX/做空门禁等) 统一由
_submit_order._check_gates_and_conflicts 执行，本阶段不再重复。

输入: SignalCandidate
输出: OrderRequest 或 None (仓位计算为 0)
"""

from __future__ import annotations

import logging
from typing import Optional, Dict, Any

from app.core.pipeline_contracts import SignalCandidate, OrderRequest

logger = logging.getLogger(__name__)


def process(candidate: SignalCandidate,
            risk_engine=None,
            position_store=None,
            execution_engine=None,
            polling_controller=None) -> Optional[OrderRequest]:
    """
    对信号候选执行风控审批 + 仓位计算。

    Args:
        candidate: SignalCandidate (来自 SignalStage)
        risk_engine: RiskEngine 实例 (含 PreTradeChecker)
        position_store: 持仓管理器 (仓位限制检查需要)
        execution_engine: 执行引擎 (下单时需要 broker_adapter)
        polling_controller: 自适应轮询控制器

    Returns:
        OrderRequest 或 None
    """
    symbol = candidate.symbol

    # ── 1. 风控门禁 ──
    # 风控门禁统一由 _submit_order._check_gates_and_conflicts 执行，避免重复。
    # _check_gates_and_conflicts 是完整门禁集合 (RiskStateMachine/ADR/VIX/做空门禁/
    # TradeGuard/动量门控/作战计划)，pre_trade_check 仅是其子集。
    # 此处不再调用 risk_engine.pre_trade_check()，防止每单 3 次重复检查。

    # ── 3. 仓位计算 ──
    quantity = _compute_position_size(symbol, candidate, risk_engine)
    if quantity <= 0:
        logger.warning(f"[Risk] {symbol} 仓位计算为 0, 跳过")
        return None

    # ── 4. 止损/止盈计算 ──
    ctx = candidate.context
    atr = ctx.atr if ctx else 0
    price = ctx.price if ctx else 0
    atr_mult = 1.5

    stop_loss = price * 0.94  # -6% 默认
    take_profit_1 = price * 1.06  # +6% 默认
    if atr > 0 and price > 0:
        stop_loss = price - (atr * atr_mult)
        take_profit_1 = price + (atr * atr_mult * 2)

    return OrderRequest(
        candidate=candidate,
        quantity=quantity,
        order_type="LIMIT",
        stop_loss=round(stop_loss, 2),
        take_profit_levels=[
            {"ratio": 0.50, "price": round(take_profit_1, 2)},
            {"ratio": 0.50, "price": round(take_profit_1 * 1.03, 2)},
        ],
        risk_approved=True,
        risk_checks_passed=[],
        risk_notes=candidate.metadata.get("risk_notes", []),
    )


def _compute_position_size(symbol: str, candidate: SignalCandidate, risk_engine) -> int:
    """计算目标仓位 (股数)"""
    try:
        from app.risk.capital_safety import get_capital_safety_system
        capital = get_capital_safety_system()
        equity = capital._state.current_equity if capital._state and capital._state.current_equity > 0 else 1_000_000.0

        # 获取 tier
        tier = "inst"
        try:
            from app.core.config_container import get_config
            tiers = get_config().get_dotted("capital.tiers") or {}
            for name in ["micro", "small", "medium", "large", "inst"]:
                t = tiers.get(name, {})
                min_e = t.get("min_equity", 0)
                max_e = t.get("max_equity", float("inf")) or float("inf")
                if min_e <= equity <= max_e:
                    tier = name
                    break
        except Exception:
            pass

        # 获取水位
        water_level = "green"
        try:
            if risk_engine and hasattr(risk_engine, 'capital_safety'):
                level = risk_engine.capital_safety.get_water_level()
                if level:
                    water_level = level
        except Exception:
            pass

        # 单笔仓位占比 (默认 5%)
        position_pct = 0.05
        risk_mult = 1.0
        if water_level == "yellow":
            risk_mult = 0.5
            position_pct = 0.025
        elif water_level in ("orange", "red"):
            risk_mult = 0.0
            return 0

        max_order_amount = equity * position_pct
        price = candidate.context.price if candidate.context else 0
        if price <= 0:
            return 0

        raw_qty = int(max_order_amount / price)
        # 取整到 10 股
        qty = max(10, (raw_qty // 10) * 10)

        logger.info(f"[Risk] {symbol} 仓位={qty}股 (${qty*price:,.0f}) | "
                    f"equity=${equity:,.0f} | tier={tier} | wl={water_level} | pct={position_pct:.1%}")

        return qty
    except Exception as e:
        logger.error(f"[Risk] {symbol} 仓位计算异常: {e}")
        return 0
