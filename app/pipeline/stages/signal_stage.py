"""
Signal Stage — 信号生成 (策略编排层)

职责:
  1. Restricted 列表检查
  2. 按优先级尝试各策略: 快车道 → 技术面 → LLM 投票
  3. 返回第一个命中的 SignalCandidate

策略本身是纯函数 (app/strategy/pure/), 本 Stage 只负责编排调用顺序。
"""

from __future__ import annotations

import logging
from typing import Optional, Dict, Any

from app.core.pipeline_contracts import SymbolContext, SignalCandidate
from app.strategy.pure import evaluate as eval_strategy

logger = logging.getLogger(__name__)


def process(ctx: SymbolContext,
            position_store=None,
            battle_plan_manager=None,
            voting_coordinator=None) -> Optional[SignalCandidate]:
    """
    对增强行情上下文运行策略链, 生成信号候选。

    编排顺序 (第一个命中即返回):
      1. Restricted 检查 → 拦截
      2. 快车道 (作战计划 entry_zone 匹配 + 无持仓)
      3. 技术面 (多指标共振, 盘中专用)
      4. LLM 投票 (盘前/盘后专用)

    Args:
        ctx: SymbolContext 增强行情快照 (来自 MarketDataStage)
        position_store: 持仓管理器 (检查是否已有持仓)
        battle_plan_manager: 作战计划管理器 (快车道数据源)
        voting_coordinator: 投票协调器 (LLM 投票数据源)

    Returns:
        SignalCandidate 或 None (无策略命中)
    """
    symbol = ctx.symbol

    # ── Restricted 拦截 (2026-05-06 临时关闭: 观察Scalping+主线执行) ──
    # try:
    #     if battle_plan_manager:
    #         plan = battle_plan_manager.get_plan()
    #         restricted = plan.get("restricted", []) if plan else []
    #         if symbol in restricted:
    #             logger.warning(f"[Signal] {symbol} 在 Restricted 列表中, 拒绝")
    #             return None
    # except Exception as e:
    #     logger.debug(f"[Signal] Restricted 检查异常 (放行): {e}")
    # TODO: 恢复后删除以上注释
    if False:  # 临时跳过全部Restricted检查
        pass

    # ── 策略 1: 快车道 (无持仓时优先检查) ──
    try:
        has_position = bool(position_store and hasattr(position_store, 'has') and position_store.has(symbol))
    except Exception:
        has_position = False
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
                            logger.info(f"[Signal] {symbol} 快车道命中: {candidate.rationale}")
                            return candidate
        except Exception as e:
            logger.debug(f"[Signal] 快车道检查异常 (放行): {e}")

    # ── 策略 2: 技术面多因子 ──
    try:
        candidate = eval_strategy("technical", ctx)
        if candidate:
            logger.info(f"[Signal] {symbol} 技术面命中: {candidate.rationale}")
            return candidate
        else:
            logger.debug(f"[Signal] {symbol} 技术面无命中")
    except Exception as e:
        logger.debug(f"[Signal] 技术面异常 (放行): {e}")

    # ── 策略 3: LLM 投票 ──
    try:
        if voting_coordinator and not has_position:
            vote_result = voting_coordinator.vote(symbol, ctx)
            if vote_result:
                ctx.metadata["vote_result"] = vote_result
                candidate = eval_strategy("llm_vote", ctx)
                if candidate:
                    logger.info(f"[Signal] {symbol} LLM投票命中: {candidate.rationale}")
                    return candidate
    except Exception as e:
        logger.debug(f"[Signal] LLM投票异常 (放行): {e}")

    logger.debug(f"[Signal] {symbol} 无策略命中")
    return None
