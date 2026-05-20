"""
Portfolio Constructor — FTSeek-US 组合构建层

Collect-Rank-Allocate 漏斗: 在每个扫描周期内收集所有通过门禁的信号候选,
按复合评分排序, 按风险预算贪婪分配, 输出最终执行计划。

核心理念: 从 "流水线顺序执行" 变为 "收集-排序-分配" — 一次架构变更, 全局生效。

复合评分 = strength×0.35 + confidence×0.25 + session_alignment×0.20 + direction_balance×0.20
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Tuple

from app.core.models import SignalScore, Direction
from app.core.shared_kernel import get_shared_kernel

logger = logging.getLogger(__name__)

# ── 时段-策略兼容性 (先验: 基于各时段波动率特征) ──
# opening_drive: 高波动, 方向不确定 → 均值回归最优
# morning:       趋势确立 → 趋势跟随最优
# midday:        低量震荡 → 所有策略降权, 均值回归略好
# afternoon:     趋势延续 → 趋势跟随最优
# closing_drive: 机构调仓 → 动量/突破最优
SESSION_STRATEGY_ALIGNMENT: Dict[str, Dict[str, float]] = {
    "opening_drive": {"trend_following": 0.70, "mean_reversion": 1.00, "breakout": 0.60},
    "morning":       {"trend_following": 1.00, "mean_reversion": 0.80, "breakout": 0.90},
    "midday":        {"trend_following": 0.50, "mean_reversion": 0.70, "breakout": 0.40},
    "afternoon":     {"trend_following": 1.00, "mean_reversion": 0.70, "breakout": 0.80},
    "closing_drive": {"trend_following": 1.00, "mean_reversion": 0.50, "breakout": 0.90},
}

# ── 市场状态 → 净敞口目标 (正=LONG偏好, 负=SHORT偏好) ──
REGIME_NET_EXPOSURE_TARGET: Dict[str, float] = {
    "trending_up":    0.30,   # 牛市中偏多, net 30% long
    "trending_down": -0.20,   # 熊市中偏空, net 20% short
    "range":           0.00,   # 震荡市中性
    "high_vol":       -0.10,   # 高波动轻微偏空 (波动率风险溢价)
    "crisis":          0.00,   # 危机模式禁止开仓
    "normal":          0.10,   # 正常轻度偏多 (美股长期向上)
}

# 复合评分权重 (和为1.0)
# P1 G2: 新增 plan_alignment=0.15, 从 session/direction 各减 0.05
SCORE_WEIGHTS = {
    "strength":   0.30,
    "confidence": 0.20,
    "session":    0.20,
    "direction":  0.15,
    "plan":       0.15,
}

# P1 G8: Session-aware 净敞口目标修正系数
# 开盘方向不确定 → 收缩敞口偏好; 收盘趋势确立 → 放大敞口偏好
SESSION_EXPOSURE_TARGET_MODIFIER: Dict[str, float] = {
    "opening_drive": 0.50,
    "morning":       0.80,
    "midday":        0.60,
    "afternoon":     1.00,
    "closing_drive": 1.20,
}


@dataclass
class Candidate:
    """信号候选 — 通过所有门禁后进入候选池"""
    symbol: str
    signal: SignalScore
    context: Dict[str, Any]
    vote_result: Optional[Dict[str, Any]] = None
    battle_plan_direction: str = ""  # P1 G2: 作战计划方向 (LONG/SHORT/ANY/"")
    composite_score: float = 0.0
    allocated_risk: float = 0.0
    allocated_size: int = 0
    rank: int = -1


class PortfolioConstructor:
    """组合构建器: 收集 → 评分 → 排序 → 分配"""

    def __init__(self):
        self._candidates: List[Candidate] = []

    # ── Phase 1: 收集 ──

    def reset(self):
        """每个扫描周期开始时清空候选池"""
        self._candidates.clear()

    def add_candidate(self, symbol: str, signal: SignalScore,
                      context: Dict[str, Any],
                      vote_result: Dict[str, Any] = None,
                      battle_plan_direction: str = ""):
        """添加通过门禁的信号候选

        P1 G2: battle_plan_direction 来自作战计划, 用于 plan_alignment 评分
        """
        candidate = Candidate(
            symbol=symbol,
            signal=signal,
            context=context,
            vote_result=vote_result,
            battle_plan_direction=battle_plan_direction,
        )
        self._candidates.append(candidate)
        logger.info(f"[组合构建] 候选入池: {symbol} "
                     f"dir={signal.direction.value} "
                     f"plan_dir={battle_plan_direction or 'N/A'} "
                     f"strength={signal.strength} conf={signal.confidence:.2f}")

    @property
    def candidate_count(self) -> int:
        return len(self._candidates)

    # ── Phase 2: 评分 ──

    def score_candidates(self, current_session: str,
                         vix_regime: str,
                         current_net_exposure: float,
                         current_total_exposure: float):
        """对所有候选计算复合评分

        P1 G8: Session-aware net exposure target — 开盘收缩偏好, 收盘放大
        """
        net_target = REGIME_NET_EXPOSURE_TARGET.get(vix_regime, 0.10)
        # P1 G8: 应用 session modifier
        session_mod = SESSION_EXPOSURE_TARGET_MODIFIER.get(current_session, 1.0)
        net_target = net_target * session_mod
        exposure_gap = net_target - current_net_exposure

        logger.info(
            f"[组合评分] regime={vix_regime} session={current_session} "
            f"net_target={net_target:+.2f} (base={net_target/session_mod:+.2f} × {session_mod:.2f}) "
            f"current_net={current_net_exposure:+.2f} gap={exposure_gap:+.2f}"
        )

        for c in self._candidates:
            c.composite_score = self._compute_composite(
                c, current_session, exposure_gap, current_total_exposure
            )

        # 按复合评分降序排序
        self._candidates.sort(key=lambda x: x.composite_score, reverse=True)
        for i, c in enumerate(self._candidates):
            c.rank = i + 1
            logger.info(f"[组合评分] #{c.rank} {c.symbol} score={c.composite_score:.3f} "
                         f"dir={c.signal.direction.value}")

    def _compute_composite(self, c: Candidate, session: str,
                           exposure_gap: float,
                           total_exposure: float) -> float:
        """计算单个候选的复合评分 (0-1 范围)

        P1 G2: 新增 plan_alignment 维度 — 作战计划方向匹配加分
        """
        # 1. 信号强度 (归一化到 0-1)
        strength_norm = max(0.0, min(1.0, c.signal.strength / 100.0))

        # 2. 置信度 (已在 0-1)
        confidence = max(0.0, min(1.0, c.signal.confidence))

        # 3. 时段-策略兼容性
        strategy_type = self._infer_strategy_type(c)
        session_alignment = SESSION_STRATEGY_ALIGNMENT.get(
            session, {}
        ).get(strategy_type, 0.70)

        # 4. 方向平衡: 朝着净敞口目标方向加分, 背离减分
        direction_bonus = self._direction_balance_score(
            c.signal.direction, exposure_gap, total_exposure
        )

        # 5. P1 G2: 作战计划方向匹配
        plan_alignment = self._plan_alignment_score(c)

        score = (
            strength_norm    * SCORE_WEIGHTS["strength"] +
            confidence       * SCORE_WEIGHTS["confidence"] +
            session_alignment * SCORE_WEIGHTS["session"] +
            direction_bonus  * SCORE_WEIGHTS["direction"] +
            plan_alignment   * SCORE_WEIGHTS["plan"]
        )
        return round(score, 4)

    @staticmethod
    def _infer_strategy_type(c: Candidate) -> str:
        """推断信号策略类型: trend_following | mean_reversion | breakout"""
        ctx = c.context
        rsi = float(ctx.get("rsi", 50))
        bb_position = float(ctx.get("bb_position", 0.5))

        # 突破型: RSI 极端 + 布林带外
        if (rsi > 80 or rsi < 20) and (bb_position > 0.9 or bb_position < 0.1):
            return "breakout"

        # 均值回归型: RSI 超买/超卖 + 价格在布林带内
        if (rsi > 65 or rsi < 35) and 0.2 <= bb_position <= 0.8:
            return "mean_reversion"

        # 趋势跟随型: RSI 中等 + MACD 同向
        macd = float(ctx.get("macd", 0))
        if (rsi > 50 and macd > 0) or (rsi < 50 and macd < 0):
            return "trend_following"

        return "trend_following"  # 默认

    @staticmethod
    def _plan_alignment_score(c: Candidate) -> float:
        """P1 G2: 作战计划方向匹配评分 (0-1)

        - 信号方向与作战计划一致 → 1.0 (高优先级)
        - 作战计划为 ANY 或无计划 → 0.70 (中性)
        - 信号方向与计划背离 → 0.30 (需要极强技术面才能覆盖)
        - 不在作战计划中 → 0.50 (后门, 需技术面足够强)
        """
        plan_dir = c.battle_plan_direction.upper()
        signal_dir = c.signal.direction.value  # "LONG" or "SHORT"

        if not plan_dir:
            return 0.70  # 无计划信息, 中性

        if plan_dir == "ANY":
            return 0.70

        if plan_dir == signal_dir:
            return 1.0  # 完美匹配

        # 方向背离
        if plan_dir in ("LONG", "SHORT") and signal_dir in ("LONG", "SHORT"):
            return 0.30  # 背离

        return 0.50  # 不在计划中

    @staticmethod
    def _direction_balance_score(direction: Direction,
                                 exposure_gap: float,
                                 total_exposure: float) -> float:
        """
        方向平衡评分 (0-1)

        exposure_gap > 0: 需要更多 LONG
        exposure_gap < 0: 需要更多 SHORT
        """
        if direction == Direction.LONG:
            if exposure_gap > 0:
                # 需要 LONG, 加分
                return min(1.0, 0.5 + exposure_gap)
            else:
                # 需要 SHORT 但信号是 LONG, 减分
                return max(0.0, 0.5 + exposure_gap)
        else:  # SHORT
            if exposure_gap < 0:
                # 需要 SHORT, 加分
                return min(1.0, 0.5 - exposure_gap)
            else:
                # 需要 LONG 但信号是 SHORT, 减分
                return max(0.0, 0.5 - exposure_gap)

    # ── Phase 3: 分配 ──

    def allocate_risk_budget(self, capital_safety,
                             max_positions: int,
                             existing_positions: List[Any]) -> List[Candidate]:
        """
        按评分排序贪婪分配风险预算

        Returns:
            获得分配的执行计划 (按优先级排序)
        """
        state = capital_safety._state
        total_equity = state.current_equity if state.current_equity > 0 else 100000.0
        risk_multiplier = capital_safety.get_risk_multiplier()
        total_risk_budget = total_equity * capital_safety.config.max_total_risk_pct * risk_multiplier

        # 扣除已有持仓的已占用风险
        already_allocated = self._estimate_allocated_risk(existing_positions, total_equity)
        remaining_budget = max(0.0, total_risk_budget - already_allocated)
        available_slots = max(0, max_positions - len(existing_positions))

        logger.info(
            f"[风险预算] 总预算=${total_risk_budget:,.0f} | "
            f"已占用=${already_allocated:,.0f} | "
            f"剩余=${remaining_budget:,.0f} | "
            f"可用槽位={available_slots}/{max_positions}"
        )

        if available_slots <= 0:
            logger.info("[组合分配] 无可用槽位, 跳过分配")
            return []

        if remaining_budget <= 0:
            logger.info("[组合分配] 无剩余风险预算, 跳过分配")
            return []

        execution_plan = []
        for c in self._candidates:
            if len(execution_plan) >= available_slots:
                break
            if remaining_budget <= 0:
                break

            # 计算此候选的仓位 — P0 修复: ATR 动态止损替代固定 10%
            price = float(c.context.get("price", 0))
            if price <= 0:
                logger.warning(f"[组合分配] {c.symbol} 无有效价格, 跳过")
                continue

            # ATR 动态止损: 使用 session-aware effective ATR
            atr = float(c.context.get("atr", 0))
            direction = c.signal.direction.value if hasattr(c.signal.direction, 'value') else str(c.signal.direction)
            is_short = (direction == "SHORT")

            # session-aware ATR (与 ExitMonitor 一致)
            kernel = get_shared_kernel()
            session = kernel.get_current_session()
            session_ratio = kernel.get_session_vol_ratio(session)
            effective_atr = atr * session_ratio if atr > 0 else 0

            # 动态止损距离: 2.5x effective ATR (与 exit_policy.yaml atr_stop.initial 一致)
            if effective_atr > 0:
                if is_short:
                    stop_loss_price = price + effective_atr * 2.5
                    # 硬底限: 最大亏损 15%
                    stop_loss_price = min(stop_loss_price, price * 1.15)
                else:
                    stop_loss_price = price - effective_atr * 2.5
                    # 硬底限: 最大亏损 15%
                    stop_loss_price = max(stop_loss_price, price * 0.85)
            else:
                # ATR 不可用时的降级方案 (P0-4 修复: SHORT 方向止损必须在上方)
                if is_short:
                    stop_loss_price = price * 1.10  # SHORT: 止损在入场价上方 10%
                else:
                    stop_loss_price = price * 0.90  # LONG: 止损在入场价下方 10%

            sizing = capital_safety.calc_position_size(
                entry_price=price,
                stop_loss_price=stop_loss_price,
            )
            size = sizing.get("size", 0)
            risk_amount = sizing.get("risk_amount", 0)

            if size <= 0:
                logger.info(f"[组合分配] {c.symbol} 仓位为0 ({sizing.get('reason')}), 跳过")
                continue

            if risk_amount > remaining_budget and len(execution_plan) > 0:
                logger.info(f"[组合分配] {c.symbol} 需风险${risk_amount:,.0f} "
                             f"> 剩余${remaining_budget:,.0f}, 跳过 (已有{len(execution_plan)}个分配)")
                continue

            c.allocated_size = size
            c.allocated_risk = risk_amount
            remaining_budget -= risk_amount
            execution_plan.append(c)

            logger.info(
                f"[组合分配] ✅ #{c.rank} {c.symbol} "
                f"size={size} risk=${risk_amount:,.0f} "
                f"score={c.composite_score:.3f} "
                f"剩余预算=${remaining_budget:,.0f}"
            )

        logger.info(f"[组合分配] 执行计划: {len(execution_plan)} 笔交易, "
                     f"淘汰 {len(self._candidates) - len(execution_plan)} 个候选")
        return execution_plan

    @staticmethod
    def _estimate_allocated_risk(positions: List[Any], total_equity: float) -> float:
        """估算已有持仓占用的风险金额"""
        if not positions:
            return 0.0
        total_risk = 0.0
        for p in positions:
            size = getattr(p, 'size', 0) or 0
            entry = getattr(p, 'avg_entry_price', 0) or 0
            sl = getattr(p, 'current_sl', 0) or 0
            if sl > 0 and entry > 0 and size > 0:
                risk_per_share = abs(entry - sl)
                total_risk += size * risk_per_share
        return total_risk

    def get_all_candidates(self) -> List[Candidate]:
        return list(self._candidates)

    # ── P2 G13: 从 DecisionRecorder 学习数据更新时段-策略对齐 ──

    @classmethod
    def apply_learned_alignment(cls, learned: Dict[str, Dict[str, float]],
                                blend: float = 0.5):
        """从 DecisionRecorder.learn_session_strategy_alignment() 应用学习结果

        blend=0.5 表示学习值和先验值各占 50%, 平滑过渡避免过拟合。
        blend=1.0 表示完全替换为先验值 (reset)。

        Args:
            learned: {"opening_drive": {"trend_following": 0.85, ...}, ...}
            blend: 0.0=保持先验, 1.0=完全替换为学习值
        """
        if not learned:
            return

        changes = 0
        for session, strategies in learned.items():
            if session not in SESSION_STRATEGY_ALIGNMENT:
                SESSION_STRATEGY_ALIGNMENT[session] = {}
            for strategy, learned_score in strategies.items():
                prior = SESSION_STRATEGY_ALIGNMENT[session].get(strategy, 0.70)
                blended = round(prior * (1 - blend) + learned_score * blend, 2)
                if abs(blended - prior) > 0.01:
                    SESSION_STRATEGY_ALIGNMENT[session][strategy] = blended
                    changes += 1

        logger.info(
            f"[组合构建] 时段-策略对齐已更新: {changes} 处变更 (blend={blend:.0%})"
        )


# ── 模块级单例 ──

_portfolio_constructor: Optional[PortfolioConstructor] = None


def get_portfolio_constructor() -> PortfolioConstructor:
    global _portfolio_constructor
    if _portfolio_constructor is None:
        _portfolio_constructor = PortfolioConstructor()
    return _portfolio_constructor
