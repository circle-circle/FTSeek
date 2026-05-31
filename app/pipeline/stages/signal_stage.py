"""
Signal Stage — 信号生成 (策略编排层)

职责:
  1. 收集所有策略候选 (collect-all) + 质量评分 + 信号融合
  2. 快车道 → ORB → 趋势优先 → 技术面 → LLM 投票
  3. 同方向多策略汇合 → 置信度加成; 方向冲突 → 取最强 + 惩罚

策略本身是纯函数 (app/strategy/pure/), 本 Stage 只负责编排调用顺序。
"""

from __future__ import annotations

import logging
from typing import Optional

from app.core.pipeline_contracts import SymbolContext, SignalCandidate
from app.strategy.pure import evaluate as eval_strategy

logger = logging.getLogger(__name__)


def _apply_quality_scoring(ctx: SymbolContext, candidate: SignalCandidate) -> None:
    """对单个候选施加 SignalQualityScorer 评分, 就地修改 confidence/strength"""
    try:
        from app.strategy.signal_quality_scorer import SignalQualityScorer
        direction_str = candidate.direction if isinstance(candidate.direction, str) else (
            candidate.direction.value if hasattr(candidate.direction, 'value') else str(candidate.direction)
        )
        quality = SignalQualityScorer.score(ctx, direction_str)
        candidate.confidence = quality.confidence
        candidate.strength = quality.strength
        if isinstance(getattr(candidate, 'metadata', None), dict):
            candidate.metadata["quality_factors"] = quality.to_dict()
    except Exception as qe:
        logger.debug(f"[SignalStage] quality scoring failed: {qe}")


def process(ctx: SymbolContext,
            position_store=None,
            battle_plan_manager=None,
            voting_coordinator=None) -> Optional[SignalCandidate]:
    """
    策略编排: collect-all + 质量评分 + 信号融合.

    顺序:
      1. 快车道 (作战计划匹配, 最高优先级)
      2. ORB (开盘区间突破, 09:45-11:00)
      3. Trend-First (趋势方向 + RSI回调, 10:00+)
      4. Technical (均值回归多因子)
      5. LLM 投票 (盘前/盘后)

    所有命中策略收集后:
      - 每个候选施加 SignalQualityScorer 多维评分
      - 同方向多策略 → 汇合加成 (confluence bonus)
      - 方向冲突 → 取最强候选 + 惩罚
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

    candidates: list[SignalCandidate] = []

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
                        if candidate is not None:
                            _apply_quality_scoring(ctx, candidate)
                            logger.info(
                                f"[Signal] {symbol} 快车道命中: {candidate.rationale}"
                            )
                            candidates.append(candidate)
        except Exception as e:
            logger.debug(f"[Signal] 快车道检查异常 (放行): {e}")

    # ── 策略 2: ORB (开盘区间突破, 无持仓时) ──
    if not has_position:
        try:
            candidate = eval_strategy("orb", ctx)
            if candidate is not None:
                _apply_quality_scoring(ctx, candidate)
                logger.info(
                    f"[Signal] {symbol} ORB命中: {candidate.rationale}"
                )
                candidates.append(candidate)
        except Exception as e:
            logger.debug(f"[Signal] ORB异常 (放行): {e}")

    # ── 策略 3: Trend-First (趋势方向 + RSI回调) ──
    try:
        candidate = eval_strategy("trend_first", ctx)
        if candidate is not None:
            _apply_quality_scoring(ctx, candidate)
            logger.info(
                f"[Signal] {symbol} TrendFirst命中: {candidate.rationale}"
            )
            candidates.append(candidate)
    except Exception as e:
        logger.debug(f"[Signal] TrendFirst异常 (放行): {e}")

    # ── 策略 4: Technical (均值回归多因子) ──
    try:
        candidate = eval_strategy("technical", ctx)
        if candidate is not None:
            _apply_quality_scoring(ctx, candidate)
            logger.info(
                f"[Signal] {symbol} 技术面命中: {candidate.rationale}"
            )
            candidates.append(candidate)
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
                if candidate is not None:
                    _apply_quality_scoring(ctx, candidate)
                    logger.info(
                        f"[Signal] {symbol} LLM投票命中: {candidate.rationale}"
                    )
                    candidates.append(candidate)
    except Exception as e:
        logger.debug(f"[Signal] LLM投票异常 (放行): {e}")

    # ── 无候选 → 返回 None ──
    if not candidates:
        logger.debug(f"[Signal] {symbol} 无策略命中")
        return None

    # ── 单候选 → 直接返回 (已施加质量评分) ──
    if len(candidates) == 1:
        return candidates[0]

    # ── 信号融合: 多策略命中 ──
    directions = []
    for c in candidates:
        d = c.direction if isinstance(c.direction, str) else (
            c.direction.value if hasattr(c.direction, 'value') else str(c.direction)
        )
        directions.append(d)

    if len(set(directions)) == 1:
        # 所有候选同方向 → 汇合加成 (confluence bonus)
        best = max(candidates, key=lambda c: c.confidence)
        bonus = 0.08 * (len(candidates) - 1)
        best.confidence = min(0.95, best.confidence + bonus)
        best.strength = min(99, best.strength + 5 * (len(candidates) - 1))
        if isinstance(getattr(best, 'metadata', None), dict):
            best.metadata["fused"] = True
            best.metadata["fused_count"] = len(candidates)
            best.metadata["fused_strategies"] = [c.strategy_id for c in candidates]
        logger.info(
            f"[SignalFusion] {symbol} {directions[0]} "
            f"fused {len(candidates)} strategies → conf={best.confidence:.3f}"
        )
        return best

    # 方向冲突 → 取最强候选 + 惩罚
    best = max(candidates, key=lambda c: c.confidence)
    best.confidence = max(0.30, best.confidence - 0.05)
    if isinstance(getattr(best, 'metadata', None), dict):
        best.metadata["conflict"] = True
    logger.info(
        f"[SignalFusion] {symbol} conflict "
        f"({', '.join(directions)}), taking strongest with penalty"
    )
    return best
