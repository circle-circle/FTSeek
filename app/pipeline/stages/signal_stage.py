"""
Signal Stage — 信号生成 (策略编排层)

职责:
  1. Restricted 列表检查
  2. 按优先级尝试各策略: 快车道 → Best-Hit(TrendFirst ∥ Technical) → LLM 投票
  3. 返回第一个命中的 SignalCandidate

策略本身是纯函数 (app/strategy/pure/), 本 Stage 只负责编排调用顺序。

RFC-054: Best-Hit 模式
  - Trend-First 与 Technical 策略并行评估同一 symbol
  - 取 quality = strength × confidence 最高者作为输出
  - Fast-Lane (作战计划) 仍保持最高优先级
  - 在策略评估前注入板块数据到 ctx.metadata
"""

from __future__ import annotations

import logging
from typing import Optional, Dict, Any, Tuple, List

from app.core.pipeline_contracts import SymbolContext, SignalCandidate
from app.strategy.pure import evaluate as eval_strategy

logger = logging.getLogger(__name__)


def _inject_sector_info(ctx: SymbolContext) -> None:
    """RFC-054: 注入板块龙头信息到 ctx.metadata['sector_info']

    sector_info 结构:
      {"sector_score": float, "is_leader": bool, "sector_name": str}

    Trend-First 策略的 Layer 2 选股评分依赖该字段。若上游已注入则不覆盖。
    """
    try:
        if not hasattr(ctx, "metadata") or ctx.metadata is None:
            ctx.metadata = {}
        # 上游已注入则尊重
        if ctx.metadata.get("sector_info"):
            return

        from app.risk.sector_leader import (
            get_sector_leader_detector,
            SECTOR_MAPPING,
        )

        detector = get_sector_leader_detector()
        symbol = ctx.symbol or ""
        sym_short = symbol.replace("US.", "").upper()

        # 1) 反查个股所属板块 (取首个匹配)
        sector_name = "unknown"
        for s_name, symbols in SECTOR_MAPPING.items():
            if sym_short in symbols:
                sector_name = s_name
                break

        # 2) 从检测器最近一次 analyze_sectors 的缓存读取热度 / 龙头标识
        sector_score = 0.0
        is_leader = False
        try:
            scores = getattr(detector, "_sector_scores", {}) or {}
            info = scores.get(sector_name)
            if info is not None:
                sector_score = float(getattr(info, "score", 0.0) or 0.0)
                for leader in getattr(info, "leaders", []) or []:
                    l_sym = (leader.get("symbol") or "").replace("US.", "").upper()
                    if l_sym == sym_short:
                        is_leader = True
                        break
        except Exception as inner_e:
            logger.debug(f"[SignalStage] 板块得分读取失败 {symbol}: {inner_e}")

        ctx.metadata["sector_info"] = {
            "sector_score": sector_score,
            "is_leader": is_leader,
            "sector_name": sector_name,
        }
    except Exception as e:
        logger.debug(f"[SignalStage] 板块信息注入失败: {e}")
        if not hasattr(ctx, "metadata") or ctx.metadata is None:
            ctx.metadata = {}
        ctx.metadata.setdefault(
            "sector_info",
            {"sector_score": 0, "is_leader": False, "sector_name": "unknown"},
        )


def _eval_one(name: str, ctx: SymbolContext) -> Optional[SignalCandidate]:
    """单策略评估封装 — 异常隔离, 不影响其他策略."""
    try:
        return eval_strategy(name, ctx)
    except Exception as e:
        logger.warning(f"[BestHit] {ctx.symbol} {name} 评估异常: {e}")
        return None


def _quality(sig: SignalCandidate) -> float:
    """quality = strength × confidence — 用于 Best-Hit 排序."""
    try:
        return float(sig.strength or 0) * float(sig.confidence or 0.0)
    except Exception:
        return 0.0


def _get_strategy_cfg(name: str) -> Dict[str, Any]:
    """从配置中心读取 strategies.<name>, 失败返回空 dict."""
    try:
        from app.core.config_container import get_config
        cfg = get_config().get_dotted(f"strategies.{name}") or {}
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _best_hit_evaluate(ctx: SymbolContext) -> Optional[SignalCandidate]:
    """RFC-054: Best-Hit 模式 — TrendFirst ∥ Technical 并行评估, 取 quality 最高.

    observe_only 处理:
      - 当 trend_first 胜出且 cfg.observe_only=True 时, 仅记录日志, 回退到 Technical 结果
    """
    candidates: List[Tuple[str, SignalCandidate, float]] = []

    # 评估 Trend-First (RFC-054)
    tf_sig = _eval_one("trend_first", ctx)
    if tf_sig is not None:
        q = _quality(tf_sig)
        candidates.append(("trend_first", tf_sig, q))
        logger.info(
            f"[BestHit] {ctx.symbol} TrendFirst: "
            f"str={tf_sig.strength} conf={tf_sig.confidence:.2f} q={q:.1f}"
        )

    # 评估 Technical (传统多因子)
    tech_sig = _eval_one("technical", ctx)
    if tech_sig is not None:
        q = _quality(tech_sig)
        candidates.append(("technical", tech_sig, q))
        logger.info(
            f"[BestHit] {ctx.symbol} Technical: "
            f"str={tech_sig.strength} conf={tech_sig.confidence:.2f} q={q:.1f}"
        )

    if not candidates:
        logger.debug(f"[BestHit] {ctx.symbol} 无候选信号")
        return None

    # quality 最高者胜出
    candidates.sort(key=lambda x: x[2], reverse=True)
    winner_name, winner_sig, winner_q = candidates[0]

    # ── RFC-054 补丁: 方向冲突拦截 ──
    # 当两个策略方向相反且 quality 差距 < 30% 时,
    # 说明市场方向不明确, 放弃交易; 优势明确则采纳优势方。
    if len(candidates) >= 2:
        winner_dir_raw = getattr(candidates[0][1], 'direction', '')
        runner_dir_raw = getattr(candidates[1][1], 'direction', '')

        def _norm_dir(d):
            d = str(d).upper()
            if 'LONG' in d or 'BUY' in d:
                return 'LONG'
            if 'SHORT' in d or 'SELL' in d:
                return 'SHORT'
            return d

        w_dir = _norm_dir(winner_dir_raw)
        r_dir = _norm_dir(runner_dir_raw)
        if w_dir != r_dir:
            w_q = candidates[0][2]
            r_q = candidates[1][2]
            divergence = (w_q - r_q) / (w_q + 1e-8)
            if divergence < 0.30:
                logger.warning(
                    f"[BestHit] {ctx.symbol} 方向冲突拦截: "
                    f"{candidates[0][0]}={w_dir}(q={w_q:.1f}) vs "
                    f"{candidates[1][0]}={r_dir}(q={r_q:.1f}) "
                    f"divergence={divergence:.2f}<0.30 → 放弃信号"
                )
                return None
            else:
                logger.info(
                    f"[BestHit] {ctx.symbol} 方向分歧但优势明确: "
                    f"{candidates[0][0]}={w_dir}(q={w_q:.1f}) >> "
                    f"{candidates[1][0]}={r_dir}(q={r_q:.1f}) "
                    f"divergence={divergence:.2f}>=0.30 → 采纳优势方"
                )

    # observe_only: TrendFirst 仅观察, 不允许产出实际下单信号 → 回退 Technical
    tf_cfg = _get_strategy_cfg("trend_first")
    observe_only = bool(tf_cfg.get("observe_only", True))
    if winner_name == "trend_first" and observe_only:
        logger.info(
            f"[BestHit] {ctx.symbol} TrendFirst 胜出(q={winner_q:.1f}) "
            f"但 observe_only=true, 回退到 Technical"
        )
        for name, sig, q in candidates:
            if name == "technical":
                logger.info(
                    f"[BestHit] {ctx.symbol} 回退命中: technical (q={q:.1f})"
                )
                return sig
        return None

    logger.info(f"[BestHit] {ctx.symbol} 胜出: {winner_name} (q={winner_q:.1f})")
    return winner_sig


def process(ctx: SymbolContext,
            position_store=None,
            battle_plan_manager=None,
            voting_coordinator=None) -> Optional[SignalCandidate]:
    """
    对增强行情上下文运行策略链, 生成信号候选。

    编排顺序 (第一个命中即返回):
      1. Restricted 检查 → 拦截 (当前临时关闭)
      2. 快车道 (作战计划 entry_zone 匹配 + 无持仓) — 最高优先级
      3. Best-Hit: TrendFirst ∥ Technical 并行, 取 quality 最高 (RFC-054)
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

    # ── 策略 1: 快车道 (无持仓时优先检查) — 最高优先级, 命中直接返回 ──
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

    # ── 策略 2: Best-Hit (TrendFirst ∥ Technical) — RFC-054 ──
    # 评估前注入板块信息, 供 Trend-First Layer 2 选股评分使用
    _inject_sector_info(ctx)

    try:
        candidate = _best_hit_evaluate(ctx)
        if candidate:
            logger.info(
                f"[Signal] {symbol} BestHit命中 ({candidate.strategy_id}): "
                f"{candidate.rationale}"
            )
            return candidate
        else:
            logger.debug(f"[Signal] {symbol} BestHit 无命中")
    except Exception as e:
        logger.debug(f"[Signal] BestHit 异常 (放行): {e}")

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
