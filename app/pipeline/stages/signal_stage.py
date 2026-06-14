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
        quality = SignalQualityScorer.score(
            ctx, direction_str,
            strategy_confidence=getattr(candidate, 'confidence', None),
        )
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

    # ── 策略 4: Technical (均值回归多因子) — 可配置冻结 ──
    _technical_enabled = True
    try:
        from app.core.config_container import get_config
        _cfg = get_config()
        _technical_enabled = bool(_cfg.get_dotted("strategies.technical.enabled", True))
    except Exception:
        pass
    if _technical_enabled:
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

    # ── 策略 5: LLM 投票 (无持仓时) — 可配置冻结 ──
    _llm_vote_enabled = True
    try:
        from app.core.config_container import get_config
        _cfg = get_config()
        _llm_vote_enabled = bool(_cfg.get_dotted("strategies.llm_vote.enabled", True))
    except Exception:
        pass
    if _llm_vote_enabled:
        try:
            if voting_coordinator and not has_position:
                # Skip LLM vote if we already have high-confidence candidates
                if candidates and any(c.confidence >= 0.70 for c in candidates):
                    logger.info(f"[SignalStage] Skipping LLM vote for {symbol} - already have high-confidence candidates")
                else:
                    # LLM vote with timeout
                    try:
                        import concurrent.futures
                        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                            future = executor.submit(voting_coordinator.vote, symbol, ctx)
                            vote_result = future.result(timeout=5.0)  # 5s timeout — LLM 盘中投票已废弃, 盘前 battle plan 取代
                            if vote_result:
                                ctx.metadata["vote_result"] = vote_result
                                candidate = eval_strategy("llm_vote", ctx)
                                if candidate is not None:
                                    _apply_quality_scoring(ctx, candidate)
                                    logger.info(
                                        f"[Signal] {symbol} LLM投票命中: {candidate.rationale}"
                                    )
                                    candidates.append(candidate)
                    except concurrent.futures.TimeoutError:
                        logger.warning(f"[SignalStage] LLM vote timeout for {symbol}")
                    except Exception as e:
                        logger.debug(f"[SignalStage] LLM vote failed for {symbol}: {e}")
        except Exception as e:
            logger.debug(f"[Signal] LLM投票异常 (放行): {e}")

    # ── 无候选 → 返回 None ──
    if not candidates:
        logger.debug(f"[Signal] {symbol} 无策略命中")
        return None

    # ── P0 (2026-06-04): 质量底线过滤 — 防御纵深 ──
    # 修复: 原用 AND 逻辑(strength<30 AND conf<0.30 才拦)，strength=10+conf=0.35 能过关
    # 改为 OR 逻辑: strength<30 OR confidence<0.30 任一不足即过滤
    # 防止低质量信号通过融合加成后"洗白"
    _min_strength = 30
    _min_confidence = 0.30
    filtered = []
    for c in candidates:
        _s = int(getattr(c, 'strength', 0) or 0)
        _cf = float(getattr(c, 'confidence', 0.0) or 0.0)
        if _s < _min_strength or _cf < _min_confidence:
            logger.info(
                f"[Signal] {symbol} 弱信号过滤: strategy={c.strategy_id} "
                f"strength={_s}<{_min_strength} OR conf={_cf:.2f}<{_min_confidence}"
            )
        else:
            filtered.append(c)
    if not filtered:
        logger.info(f"[Signal] {symbol} 所有候选未通过质量底线 (strength≥{_min_strength} AND conf≥{_min_confidence})")
        return None
    candidates = filtered

    # ── P0 (2026-06-14): SPY 方向门禁 — 牛市抑制 SHORT 噪音 ──
    # 数据驱动: LONG IC=0.188(p<0.05), SHORT IC=-0.056(噪音)
    # 84% 信号是 SHORT 但 SHORT 无预测力 → SPY>MA200 时过滤弱 SHORT
    # 极端 SHORT 信号 (strength≥70, 即 RSI>80+单日暴涨) 仍保留
    _spy_below = getattr(ctx, 'spy_below_ma200', False)
    if not _spy_below and len(filtered) > 0:
        _short_filtered = []
        for c in candidates:
            _dir = c.direction if isinstance(c.direction, str) else (
                c.direction.value if hasattr(c.direction, 'value') else str(c.direction)
            )
            if _dir == "SHORT":
                _str = int(getattr(c, 'strength', 0) or 0)
                if _str < 70:
                    logger.info(
                        f"[Signal] {symbol} SHORT 噪音过滤 (SPY>MA200): "
                        f"strategy={c.strategy_id} strength={_str}<70"
                    )
                    continue
            _short_filtered.append(c)
        if _short_filtered:
            candidates = _short_filtered
        # 如果全部 SHORT 被过滤且无 LONG 候选, candidates 为空 → 下方自然返回 None

    # ── 单候选 → 直接返回 (已施加质量评分) ──
    if len(candidates) == 1:
        _single = candidates[0]
        # P0 (2026-06-05): 确保 owner_strategy 写入 metadata (下游 order_executor 依赖)
        if isinstance(getattr(_single, 'metadata', None), dict):
            if not _single.metadata.get("owner_strategy"):
                _single.metadata["owner_strategy"] = getattr(_single, 'strategy_id', '')
        return _single

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
            # P0 (2026-06-05): 保留获胜者的策略身份，防止被下游覆盖为 "voting_llm"
            best.metadata["owner_strategy"] = getattr(best, 'strategy_id', '') or best.metadata.get("owner_strategy", "")
            # 汇总所有候选的 owner_strategy (用于归因分析)
            _all_owners = list({getattr(c, 'strategy_id', '') for c in candidates if getattr(c, 'strategy_id', '')})
            if _all_owners:
                best.metadata["contributing_strategies"] = _all_owners
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
        # P0 (2026-06-05): 冲突路径也保留策略身份
        best.metadata["owner_strategy"] = getattr(best, 'strategy_id', '') or best.metadata.get("owner_strategy", "")
    logger.info(
        f"[SignalFusion] {symbol} conflict "
        f"({', '.join(directions)}), taking strongest with penalty"
    )
    return best
