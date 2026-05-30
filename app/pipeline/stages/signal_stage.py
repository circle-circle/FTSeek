"""
Signal Stage — 信号生成 (策略编排层)

职责:
  1. 按优先级尝试各策略, 第一个命中即返回 (first-hit)
  2. 快车道 → ORB → 趋势优先 → 技术面 → LLM 投票

策略本身是纯函数 (app/strategy/pure/), 本 Stage 只负责编排调用顺序。
"""

from __future__ import annotations

import logging
from typing import Optional

from app.core.pipeline_contracts import SymbolContext, SignalCandidate
from app.strategy.pure import evaluate as eval_strategy

logger = logging.getLogger(__name__)


def process(ctx: SymbolContext,
            position_store=None,
            battle_plan_manager=None,
            voting_coordinator=None) -> Optional[SignalCandidate]:
    """
    策略编排: first-hit returns.

    顺序:
      1. 快车道 (作战计划匹配, 最高优先级)
      2. ORB (开盘区间突破, 09:45-11:00)
      3. Trend-First (趋势方向 + RSI回调, 10:00+)
      4. Technical (均值回归多因子)
      5. LLM 投票 (盘前/盘后)
    """
    symbol = ctx.symbol

    # ── 持仓检查 (快车道和LLM投票共用) ──
    try:
        has_position = bool(
            position_store
            and hasattr(position_store, 'has')
            and position_store.has(symbol)
        )
    except Exception:
        has_position = False

    # ── 策略 1: 快车道 (作战计划, 无持仓时) ──
    if not has_position:
        try:
            if battle_plan_manager:
                target = battle_plan_manager.get_target_info(symbol)
                if target:
                    if target.get("status") == "CANCELLED":
                        logger.info(f"[Signal] {symbol} 作战计划已取消")
                        return None
                    if target.get("status") == "ACTIVE":
                        ctx.metadata["battle_plan_match"] = target
                        candidate = eval_strategy("fast_lane", ctx)
                        if candidate:
                            logger.info(
                                f"[Signal] {symbol} 快车道命中: {candidate.rationale}"
                            )
                            return candidate
        except Exception as e:
            logger.debug(f"[Signal] 快车道检查异常 (放行): {e}")

    # ── 策略 2: ORB (开盘区间突破, 无持仓时) ──
    if not has_position:
        try:
            candidate = eval_strategy("orb", ctx)
            if candidate:
                logger.info(
                    f"[Signal] {symbol} ORB命中: {candidate.rationale}"
                )
                return candidate
        except Exception as e:
            logger.debug(f"[Signal] ORB异常 (放行): {e}")

    # ── 策略 3: Trend-First (趋势方向 + RSI回调) ──
    try:
        candidate = eval_strategy("trend_first", ctx)
        if candidate:
            logger.info(
                f"[Signal] {symbol} TrendFirst命中: {candidate.rationale}"
            )
            return candidate
    except Exception as e:
        logger.debug(f"[Signal] TrendFirst异常 (放行): {e}")

    # ── 策略 4: Technical (均值回归多因子) ──
    try:
        candidate = eval_strategy("technical", ctx)
        if candidate:
            logger.info(
                f"[Signal] {symbol} 技术面命中: {candidate.rationale}"
            )
            return candidate
        else:
            logger.debug(f"[Signal] {symbol} 技术面无命中")
    except Exception as e:
        logger.debug(f"[Signal] 技术面异常 (放行): {e}")

    # ── 策略 5: LLM 投票 (无持仓时) ──
    try:
        if voting_coordinator and not has_position:
            vote_result = voting_coordinator.vote(symbol, ctx)
            if vote_result:
                ctx.metadata["vote_result"] = vote_result
                candidate = eval_strategy("llm_vote", ctx)
                if candidate:
                    logger.info(
                        f"[Signal] {symbol} LLM投票命中: {candidate.rationale}"
                    )
                    return candidate
    except Exception as e:
        logger.debug(f"[Signal] LLM投票异常 (放行): {e}")

    logger.debug(f"[Signal] {symbol} 无策略命中")
    return None
