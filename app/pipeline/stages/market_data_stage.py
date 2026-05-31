"""
Market Data Stage — 行情获取 + 上下文增强

职责:
  1. 宏观闸门 (VIX >= 25 硬拦截 / SPY < MA200 仅设 flag, LONG 由 pre_trade_check 拦截)
  2. 行情数据获取 (委托给 MarketDataRouter)
  3. 动态成本注入 (spread / ADV / ATR)
  4. 财报黑名单检查
  5. VIX Regime 注入 (委托给 SharedKernel)

输入: symbol + 共享上下文 (vix, spy_context, market_sentiment)
输出: SymbolContext (增强行情快照) 或 None (被闸门拦截)
"""

from __future__ import annotations

import logging
from typing import Optional, Dict, Any

from app.core.pipeline_contracts import SymbolContext

logger = logging.getLogger(__name__)


def process(symbol: str,
            data_router,
            vix: float,
            spy_context: Dict[str, Any],
            earnings_guard=None,
            shared_kernel=None) -> Optional[SymbolContext]:
    """
    获取并增强单个标的的行情上下文。

    Args:
        symbol: 标的代码 (e.g. "US.AAPL")
        data_router: MarketDataRouter 实例 (含 Futu LV3 / TwelveData / Finnhub)
        vix: 当前 VIX 值
        spy_context: {"spy_price": ..., "spy_ma200": ...}
        earnings_guard: EarningsRiskGuard 实例 或 None
        shared_kernel: SharedKernel 实例 或 None

    Returns:
        SymbolContext 或 None (被宏观闸门/财报黑名单拦截)
    """
    # ── Layer 1: 宏观闸门 ──
    # VIX>=25: 硬拦截 (与 pre_trade_check.py P1 gate 对齐)
    if vix >= 25:
        logger.warning(f"[宏观闸门] {symbol} VIX={vix:.1f}>=25, 禁止开仓")
        return None

    # SPY < MA200: 仅设置 context flag, 不做硬拦截。
    # 方向感知的 LONG-only 拦截由 pre_trade_check.py P1 gate 权威执行,
    # 此处仅透传 flag 供下游决策使用 (SHORT 候选不受此限制)。
    spy_price = spy_context.get("spy_price", 0)
    spy_ma200 = spy_context.get("spy_ma200", 0)
    spy_below_ma200 = bool(spy_price > 0 and spy_ma200 > 0 and spy_price < spy_ma200)
    if spy_below_ma200:
        logger.info(f"[宏观闸门] {symbol} SPY ${spy_price:.2f} < MA200 ${spy_ma200:.2f}, "
                    f"设置 spy_below_ma200 flag (LONG 由 pre_trade_check 拦截, SHORT 放行)")

    # ── 行情获取 ──
    raw = data_router.get_full_market_context(symbol)
    if not raw or raw.get("price", 0) <= 0:
        logger.warning(f"[市场数据] {symbol} 行情不可用")
        return None

    raw["symbol"] = symbol

    # ── 动态成本注入 ──
    price = raw.get("price", 0)
    bid = raw.get("lv3_bid", raw.get("bid", 0))
    ask = raw.get("lv3_ask", raw.get("ask", 0))
    if bid > 0 and ask > 0 and price > 0:
        mid = (bid + ask) / 2.0
        if mid > 0:
            spread_bps = (ask - bid) / mid * 1e4
            raw["spread_bps"] = round(spread_bps, 2)
    volume = raw.get("volume", 0)
    if volume and volume > 0:
        raw["adv_shares"] = volume * 3

    # ── 财报黑名单检查 ──
    earnings_status = None
    if earnings_guard:
        earnings_status = earnings_guard.get_earnings_status(symbol)
        if earnings_status.get("is_blackout"):
            logger.info(f"[财报拦截] {symbol} 处于禁建期: {earnings_status.get('blackout_reason', '')}")
            return None

    # ── VIX Regime 注入 ──
    vix_multipliers = None
    if shared_kernel:
        try:
            vix_multipliers = shared_kernel.get_vix_multipliers()
        except Exception:
            pass

    # ── 构建增强上下文 ──
    ctx = SymbolContext.from_raw(
        symbol, raw,
        earnings_status=earnings_status,
        vix_multipliers=vix_multipliers,
    )
    ctx.vix = vix
    ctx.spy_price = spy_price
    ctx.spy_ma200 = spy_ma200
    ctx.spy_below_ma200 = spy_below_ma200
    ctx.source = "market_data_stage"

    logger.info(f"[MarketData] {symbol} price=${ctx.price:.2f} spread={ctx.spread_bps:.0f}bps "
                f"rsi={ctx.rsi:.0f} atr={ctx.atr:.2f} regime={ctx.vix_regime}")

    return ctx
