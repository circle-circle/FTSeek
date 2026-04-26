"""
仓位冲突决策引擎 (Position Conflict Resolver)

解决核心问题: 当持仓已满 (max_open_positions 已用完) 但新来一个强烈买入信号时，
系统应该如何决策: 保持现状 / 替换最弱持仓 / 延迟执行 / 拒绝信号。

架构分层:
1. 持仓评分层: 对每只现有持仓进行多维度评分
2. 新信号评估层: 对新信号进行紧迫度和影响力评估
3. 决策引擎层: 基于对比矩阵做出最终决策
4. 执行反馈层: 记录决策结果并触发后续动作

作者: FTSeek Team
日期: 2026-04-15
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import List, Optional, Dict, Any

logger = logging.getLogger("ftseek.factors.position_conflict_resolver")


# ============================================================
# 决策结果类型
# ============================================================

class ConflictDecision(str, Enum):
    """冲突决策结果"""
    HOLD = "hold"                    # 保持现有持仓, 拒绝新信号
    REPLACE = "replace"              # 替换最弱持仓
    DEFER = "defer"                  # 延迟新信号 (加入观察列表)
    PARTIAL_SELL = "partial_sell"    # 减持某持仓, 释放部分仓位
    REJECT = "reject"                # 拒绝新信号 (强度不足)


# ============================================================
# 持仓评分维度
# ============================================================

@dataclass
class PositionScore:
    """持仓评分详情"""
    code: str
    name: str
    total_score: float = 0.0         # 综合评分 (0-100)

    # 维度评分
    pnl_score: float = 0.0           # 盈亏表现 (权重 25%)
    trend_score: float = 0.0         # 趋势强度 (权重 20%)
    conviction_score: float = 0.0    # 原始信号置信度 (权重 15%)
    age_score: float = 0.0           # 持仓时长合理性 (权重 15%)
    momentum_score: float = 0.0      # 近期动能 (权重 15%)
    risk_score: float = 0.0          # 风险状态 (权重 10%)

    # 元数据
    entry_price: float = 0.0
    current_price: float = 0.0
    pnl_pct: float = 0.0
    holding_hours: float = 0.0
    original_confidence: str = ""
    stop_loss_hit: bool = False
    take_profit_near: bool = False

    # 可替换性评级
    replaceability: str = "low"      # low / medium / high

    def compute_total(self) -> float:
        """计算加权总分"""
        weights = {
            'pnl_score': 0.25,
            'trend_score': 0.20,
            'conviction_score': 0.15,
            'age_score': 0.15,
            'momentum_score': 0.15,
            'risk_score': 0.10,
        }
        scores = {
            'pnl_score': self.pnl_score,
            'trend_score': self.trend_score,
            'conviction_score': self.conviction_score,
            'age_score': self.age_score,
            'momentum_score': self.momentum_score,
            'risk_score': self.risk_score,
        }
        self.total_score = sum(scores[k] * w for k, w in weights.items())
        self.total_score = max(0.0, min(100.0, self.total_score))

        # 可替换性评级
        if self.total_score < 30 or self.stop_loss_hit:
            self.replaceability = "high"
        elif self.total_score < 50:
            self.replaceability = "medium"
        else:
            self.replaceability = "low"

        return self.total_score


@dataclass
class NewSignalAssessment:
    """新信号评估"""
    code: str
    name: str
    total_score: float = 0.0

    # 维度评分
    conviction_score: float = 0.0    # 置信度 (权重 30%)
    urgency_score: float = 0.0       # 紧迫度 (权重 25%)
    impact_score: float = 0.0        # 事件影响力 (权重 20%)
    cross_market_score: float = 0.0  # 跨市验证 (权重 15%)
    freshness_score: float = 0.0     # 新闻新鲜度 (权重 10%)

    # 元数据
    direction: str = ""              # STRONG_BUY / BUY / SELL / STRONG_SELL
    confidence: str = ""             # HIGH / MEDIUM / LOW
    urgency: str = ""                # IMMEDIATE / NEXT_OPEN / WAIT_CONFIRM
    event_type: str = ""
    sentiment_expiry_minutes: int = 0
    position_tier: str = ""          # core / satellite
    us_hk_mapping: str = ""

    def compute_total(self) -> float:
        """计算加权总分"""
        weights = {
            'conviction_score': 0.30,
            'urgency_score': 0.25,
            'impact_score': 0.20,
            'cross_market_score': 0.15,
            'freshness_score': 0.10,
        }
        scores = {
            'conviction_score': self.conviction_score,
            'urgency_score': self.urgency_score,
            'impact_score': self.impact_score,
            'cross_market_score': self.cross_market_score,
            'freshness_score': self.freshness_score,
        }
        self.total_score = sum(scores[k] * w for k, w in weights.items())
        self.total_score = max(0.0, min(100.0, self.total_score))
        return self.total_score


@dataclass
class ConflictResult:
    """冲突决策结果"""
    decision: ConflictDecision
    new_signal_score: float = 0.0
    weakest_position: Optional[PositionScore] = None
    weakest_position_score: float = 0.0
    strongest_position: Optional[PositionScore] = None
    action_code: str = ""            # 需要操作的股票代码 (买入/卖出)
    reason: str = ""
    risk_warning: str = ""
    recommended_action: str = ""     # 人类可读的建议动作
    metadata: Dict[str, Any] = field(default_factory=dict)


# ============================================================
# 决策引擎
# ============================================================

class PositionConflictResolver:
    """
    仓位冲突决策引擎

    决策矩阵:
    ┌──────────────────┬─────────────────┬──────────────────────┐
    │  新信号强度       │  最弱持仓强度    │     决策结果          │
    ├──────────────────┼─────────────────┼──────────────────────┤
    │ 极高 (>80)       │ 极低 (<30)      │ REPLACE (立即替换)    │
    │ 极高 (>80)       │ 中 (30-60)      │ REPLACE (差距足够)    │
    │ 高 (60-80)       │ 极低 (<30)      │ REPLACE               │
    │ 高 (60-80)       │ 中 (30-60)      │ 差距≥20? REPLACE : DEFER│
    │ 中 (40-60)       │ 任意            │ DEFER (观察列表)       │
    │ 低 (<40)         │ 任意            │ REJECT (强度不足)      │
    │ 任意             │ 触及止损        │ REPLACE (强制替换)     │
    │ 任意             │ 触及止盈 80%+   │ PARTIAL_SELL (减持)    │
    └──────────────────┴─────────────────┴──────────────────────┘
    """

    # 决策阈值
    REPLACE_THRESHOLD_NEW = 60.0     # 新信号至少需要这个分数才能考虑替换
    REPLACE_GAP = 15.0               # 新信号比最弱持仓高出的最小差距
    IMMEDIATE_REPLACE_THRESHOLD = 80.0  # 超过此分数立即替换 (无论持仓状态)
    DEFER_THRESHOLD = 40.0           # 低于此分数直接拒绝
    STOP_LOSS_REPLACE = True         # 触及止损线是否强制替换
    TAKE_PROFIT_SELL_RATIO = 0.8     # 止盈目标达成 80% 时考虑减持

    def __init__(self, max_positions: int = 12, max_replace_per_day: int = 2):
        self.max_positions = max_positions
        self.max_replace_per_day = max_replace_per_day
        self._replace_count_today = 0
        self._replace_date = datetime.now().strftime("%Y-%m-%d")

    def reset_daily_counter(self):
        """重置每日替换计数器"""
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self._replace_date:
            self._replace_count_today = 0
            self._replace_date = today

    def resolve(self,
                new_signal: NewSignalAssessment,
                existing_positions: List[PositionScore]) -> ConflictResult:
        """
        核心决策方法

        Args:
            new_signal: 新信号评估
            existing_positions: 现有持仓评分列表

        Returns:
            ConflictResult: 决策结果
        """
        self.reset_daily_counter()

        # 计算总分
        new_score = new_signal.compute_total()
        for pos in existing_positions:
            pos.compute_total()

        # 如果没有现有持仓，直接批准
        if not existing_positions:
            return ConflictResult(
                decision=ConflictDecision.HOLD,
                new_signal_score=new_score,
                reason="当前无持仓，新信号无需竞争",
                recommended_action=f"直接建仓 {new_signal.code}",
                metadata={"decision_type": "no_conflict"},
            )

        # 如果有空余仓位，直接批准
        if len(existing_positions) < self.max_positions:
            return ConflictResult(
                decision=ConflictDecision.HOLD,
                new_signal_score=new_score,
                reason=f"当前持仓 {len(existing_positions)}/{self.max_positions}，有空余仓位",
                recommended_action=f"开新仓 {new_signal.code} (剩余 {self.max_positions - len(existing_positions)} 个仓位)",
                metadata={"decision_type": "available_slot"},
            )

        # 排序: 最弱 → 最强
        sorted_positions = sorted(existing_positions, key=lambda p: p.total_score)
        weakest = sorted_positions[0]
        strongest = sorted_positions[-1]

        # === 规则 1: 触及止损 → 强制替换 ===
        # 检查所有持仓是否有触及止损的
        stop_loss_positions = [p for p in existing_positions if p.stop_loss_hit]
        if self.STOP_LOSS_REPLACE and stop_loss_positions:
            # 选触及止损中评分最低的
            target = min(stop_loss_positions, key=lambda p: p.total_score)
            return ConflictResult(
                decision=ConflictDecision.REPLACE,
                new_signal_score=new_score,
                weakest_position=target,
                weakest_position_score=target.total_score,
                strongest_position=strongest,
                action_code=target.code,
                reason=f"【强制替换】{target.code} 已触及止损线，无论新信号强度都必须替换",
                risk_warning=f"{target.code} 止损触发，当前 PnL={target.pnl_pct:.1f}%",
                recommended_action=f"卖出 {target.code} (止损) → 买入 {new_signal.code}",
                metadata={"decision_type": "stop_loss_replace"},
            )

        # === 规则 2: 触及止盈 80%+ → 部分减持 ===
        # 检查所有持仓是否有接近止盈的 (不仅仅是最弱)
        take_profit_positions = [p for p in existing_positions if p.take_profit_near and p.pnl_pct > 0]
        if take_profit_positions:
            # 选最接近止盈的 (PnL 最高的)
            target = max(take_profit_positions, key=lambda p: p.pnl_pct)
            return ConflictResult(
                decision=ConflictDecision.PARTIAL_SELL,
                new_signal_score=new_score,
                weakest_position=target,
                weakest_position_score=target.total_score,
                strongest_position=strongest,
                action_code=target.code,
                reason=f"【部分减持】{target.code} 已接近止盈目标 (PnL={target.pnl_pct:.1f}%)",
                recommended_action=f"减持 {target.code} 50% → 释放仓位买入 {new_signal.code}",
                metadata={"decision_type": "take_profit_partial"},
            )

        # === 规则 3: 新信号强度评估 ===

        # 新信号太弱 → 直接拒绝
        if new_score < self.DEFER_THRESHOLD:
            return ConflictResult(
                decision=ConflictDecision.REJECT,
                new_signal_score=new_score,
                weakest_position=weakest,
                weakest_position_score=weakest.total_score,
                strongest_position=strongest,
                reason=f"【拒绝】新信号强度 {new_score:.1f} 低于阈值 {self.DEFER_THRESHOLD}",
                recommended_action=f"忽略 {new_signal.code} 信号 (强度不足)",
                metadata={"decision_type": "too_weak"},
            )

        # 新信号中等 → 延迟观察
        if new_score < self.REPLACE_THRESHOLD_NEW:
            return ConflictResult(
                decision=ConflictDecision.DEFER,
                new_signal_score=new_score,
                weakest_position=weakest,
                weakest_position_score=weakest.total_score,
                strongest_position=strongest,
                reason=f"【延迟】新信号强度 {new_score:.1f} 不足以替换现有持仓 (最弱={weakest.total_score:.1f})",
                recommended_action=f"将 {new_signal.code} 加入观察列表，等待更强信号或现有持仓走弱",
                metadata={"decision_type": "defer"},
            )

        # === 规则 4: 替换决策 ===
        score_gap = new_score - weakest.total_score

        # 新信号极强 → 无条件替换
        if new_score >= self.IMMEDIATE_REPLACE_THRESHOLD:
            if self._replace_count_today >= self.max_replace_per_day:
                return ConflictResult(
                    decision=ConflictDecision.DEFER,
                    new_signal_score=new_score,
                    weakest_position=weakest,
                    weakest_position_score=weakest.total_score,
                    strongest_position=strongest,
                    reason=f"新信号极强 ({new_score:.1f})，但今日已达替换上限 ({self.max_replace_per_day}次)",
                    risk_warning="频繁换仓增加交易成本和滑点风险",
                    recommended_action=f"延迟到下一个交易日执行 {new_signal.code} 建仓",
                    metadata={"decision_type": "daily_limit_hit"},
                )

            self._replace_count_today += 1
            return ConflictResult(
                decision=ConflictDecision.REPLACE,
                new_signal_score=new_score,
                weakest_position=weakest,
                weakest_position_score=weakest.total_score,
                strongest_position=strongest,
                action_code=weakest.code,
                reason=f"【立即替换】新信号 {new_signal.code} 强度 {new_score:.1f} 极高，替换最弱持仓 {weakest.code} ({weakest.total_score:.1f})",
                recommended_action=f"卖出 {weakest.code} → 买入 {new_signal.code}",
                metadata={"decision_type": "immediate_replace"},
            )

        # 新信号较强 + 差距足够 → 替换
        if score_gap >= self.REPLACE_GAP:
            if self._replace_count_today >= self.max_replace_per_day:
                return ConflictResult(
                    decision=ConflictDecision.DEFER,
                    new_signal_score=new_score,
                    weakest_position=weakest,
                    weakest_position_score=weakest.total_score,
                    strongest_position=strongest,
                    reason=f"信号足够强 ({new_score:.1f}) 且差距 {score_gap:.1f} ≥ {self.REPLACE_GAP}，但今日替换已达上限",
                    recommended_action=f"延迟执行，明日优先处理 {new_signal.code}",
                    metadata={"decision_type": "daily_limit_hit"},
                )

            self._replace_count_today += 1
            return ConflictResult(
                decision=ConflictDecision.REPLACE,
                new_signal_score=new_score,
                weakest_position=weakest,
                weakest_position_score=weakest.total_score,
                strongest_position=strongest,
                action_code=weakest.code,
                reason=f"【替换】新信号 {new_signal.code} ({new_score:.1f}) 比最弱持仓 {weakest.code} ({weakest.total_score:.1f}) 强 {score_gap:.1f} 分",
                recommended_action=f"卖出 {weakest.code} → 买入 {new_signal.code}",
                metadata={"decision_type": "gap_replace", "score_gap": score_gap},
            )

        # 新信号较强但差距不够 → 延迟
        return ConflictResult(
            decision=ConflictDecision.DEFER,
            new_signal_score=new_score,
            weakest_position=weakest,
            weakest_position_score=weakest.total_score,
            strongest_position=strongest,
            reason=f"【延迟】新信号 {new_score:.1f} vs 最弱持仓 {weakest.total_score:.1f}，差距 {score_gap:.1f} < {self.REPLACE_GAP}",
            recommended_action=f"将 {new_signal.code} 加入观察，差距不足 {self.REPLACE_GAP} 分不执行替换",
            metadata={"decision_type": "insufficient_gap", "score_gap": score_gap},
        )


# ============================================================
# 评分工具函数
# ============================================================

def score_position_from_data(
    code: str,
    name: str,
    pnl_pct: float,
    current_price: float,
    entry_price: float,
    holding_hours: float,
    original_confidence: str = "MEDIUM",
    trend_strength: float = 50.0,      # 0-100
    momentum_1h: float = 0.0,          # 1h 涨跌幅 %
    stop_loss_pct: float = -8.0,       # 止损线 %
    take_profit_pct: float = 15.0,     # 止盈目标 %
) -> PositionScore:
    """
    从实际数据生成持仓评分

    Args:
        code: 股票代码
        name: 股票名称
        pnl_pct: 当前盈亏百分比
        current_price: 当前价格
        entry_price: 入场价格
        holding_hours: 持仓时长 (小时)
        original_confidence: 原始信号置信度 (HIGH/MEDIUM/LOW)
        trend_strength: 趋势强度 (0-100)
        momentum_1h: 最近1小时涨跌幅
        stop_loss_pct: 止损百分比
        take_profit_pct: 止盈目标百分比
    """
    score = PositionScore(
        code=code,
        name=name,
        entry_price=entry_price,
        current_price=current_price,
        pnl_pct=pnl_pct,
        holding_hours=holding_hours,
        original_confidence=original_confidence,
    )

    # 1. 盈亏评分 (25%)
    if pnl_pct >= take_profit_pct * 0.8:
        score.pnl_score = 90.0  # 接近止盈，表现优秀
    elif pnl_pct >= 5.0:
        score.pnl_score = 70.0 + (pnl_pct / take_profit_pct) * 20
    elif pnl_pct >= 0:
        score.pnl_score = 50.0 + pnl_pct * 4  # 小幅盈利
    elif pnl_pct >= stop_loss_pct * 0.8:
        score.pnl_score = 30.0 + (pnl_pct / stop_loss_pct) * 20  # 小幅亏损
    else:
        score.pnl_score = max(0, 10.0 + pnl_pct * 2)  # 大幅亏损

    # 2. 趋势评分 (20%)
    score.trend_score = trend_strength

    # 3. 置信度评分 (15%)
    confidence_map = {"HIGH": 85.0, "MEDIUM": 60.0, "LOW": 35.0}
    score.conviction_score = confidence_map.get(original_confidence, 50.0)

    # 4. 持仓时长评分 (15%)
    # 理想持仓时长: 2-48 小时 (新闻驱动信号)
    if 2 <= holding_hours <= 48:
        score.age_score = 80.0
    elif holding_hours < 2:
        score.age_score = 60.0  # 太新，还没验证
    elif holding_hours <= 72:
        score.age_score = 50.0  # 开始老化
    else:
        score.age_score = 20.0  # 太老了

    # 5. 动量评分 (15%)
    if momentum_1h > 2.0:
        score.momentum_score = 85.0
    elif momentum_1h > 0.5:
        score.momentum_score = 65.0
    elif momentum_1h > -0.5:
        score.momentum_score = 50.0
    elif momentum_1h > -2.0:
        score.momentum_score = 30.0
    else:
        score.momentum_score = 10.0

    # 6. 风险评分 (10%)
    score.stop_loss_hit = pnl_pct <= stop_loss_pct
    score.take_profit_near = pnl_pct >= take_profit_pct * 0.8

    if score.stop_loss_hit:
        score.risk_score = 5.0   # 极高风险
    elif pnl_pct < stop_loss_pct * 0.5:
        score.risk_score = 25.0  # 高风险
    elif pnl_pct < 0:
        score.risk_score = 40.0  # 中等风险
    else:
        score.risk_score = 70.0  # 安全

    score.compute_total()
    return score


def score_new_signal_from_data(
    code: str,
    name: str,
    direction: str,
    confidence: str,
    urgency: str,
    event_type: str,
    impact_score: float,
    sentiment_expiry_minutes: int,
    position_tier: str,
    us_hk_mapping: str = "",
    has_cross_market_validation: bool = False,
    news_age_minutes: float = 0,
) -> NewSignalAssessment:
    """
    从实际数据生成新信号评分

    Args:
        code: 股票代码
        name: 股票名称
        direction: 方向 (STRONG_BUY/BUY/SELL/STRONG_SELL)
        confidence: LLM 置信度 (HIGH/MEDIUM/LOW)
        urgency: 紧迫度 (IMMEDIATE/NEXT_OPEN/WAIT_CONFIRM/MONITOR_ONLY)
        event_type: 事件类型
        impact_score: 影响力评分 (0-1)
        sentiment_expiry_minutes: 情绪有效期 (分钟)
        position_tier: 仓位等级 (core/satellite)
        us_hk_mapping: 美股映射信息
        has_cross_market_validation: 是否有跨市验证
        news_age_minutes: 新闻年龄 (分钟)
    """
    assessment = NewSignalAssessment(
        code=code,
        name=name,
        direction=direction,
        confidence=confidence,
        urgency=urgency,
        event_type=event_type,
        sentiment_expiry_minutes=sentiment_expiry_minutes,
        position_tier=position_tier,
        us_hk_mapping=us_hk_mapping,
    )

    # 1. 置信度评分 (30%)
    conf_map = {
        "STRONG_BUY": 95.0, "STRONG_SELL": 95.0,
        "BUY": 75.0, "SELL": 75.0,
        "NEUTRAL": 30.0, "WATCH_ONLY": 20.0,
    }
    base_direction = conf_map.get(direction, 50.0)
    conf_multiplier = {"HIGH": 1.0, "MEDIUM": 0.75, "LOW": 0.5}
    assessment.conviction_score = base_direction * conf_multiplier.get(confidence, 0.7)

    # 2. 紧迫度评分 (25%)
    urg_map = {
        "IMMEDIATE": 95.0,
        "NEXT_OPEN": 70.0,
        "WAIT_CONFIRM": 50.0,
        "MONITOR_ONLY": 20.0,
    }
    assessment.urgency_score = urg_map.get(urgency, 40.0)

    # 3. 影响力评分 (20%)
    assessment.impact_score = impact_score * 100.0

    # 4. 跨市验证评分 (15%)
    if has_cross_market_validation and us_hk_mapping:
        assessment.cross_market_score = 85.0
    elif us_hk_mapping:
        assessment.cross_market_score = 60.0
    else:
        assessment.cross_market_score = 30.0

    # 5. 新鲜度评分 (10%)
    if news_age_minutes < 10:
        assessment.freshness_score = 90.0
    elif news_age_minutes < 30:
        assessment.freshness_score = 70.0
    elif news_age_minutes < 60:
        assessment.freshness_score = 50.0
    elif news_age_minutes < 120:
        assessment.freshness_score = 30.0
    else:
        assessment.freshness_score = 10.0

    assessment.compute_total()
    return assessment


# ============================================================
# 钉钉推送格式化
# ============================================================

def format_conflict_alert(result: ConflictResult, new_signal: NewSignalAssessment) -> str:
    """格式化冲突决策告警 (用于钉钉推送)"""

    decision_icon = {
        ConflictDecision.HOLD: "✅",
        ConflictDecision.REPLACE: "🔄",
        ConflictDecision.DEFER: "⏳",
        ConflictDecision.PARTIAL_SELL: "📉",
        ConflictDecision.REJECT: "🚫",
    }

    decision_text = {
        ConflictDecision.HOLD: "批准建仓",
        ConflictDecision.REPLACE: "替换持仓",
        ConflictDecision.DEFER: "延迟观察",
        ConflictDecision.PARTIAL_SELL: "部分减持",
        ConflictDecision.REJECT: "拒绝信号",
    }

    icon = decision_icon.get(result.decision, "📊")
    text = decision_text.get(result.decision, "未知")

    alert = f"""### {icon} 仓位冲突决策 | {text}

**新信号**: {new_signal.code} ({new_signal.name})
**方向**: {new_signal.direction} | **置信度**: {new_signal.confidence}
**综合评分**: {result.new_signal_score:.1f}/100

"""

    if result.weakest_position:
        alert += f"""---

**最弱持仓**: {result.weakest_position.code} ({result.weakest_position.name})
**持仓评分**: {result.weakest_position_score:.1f}/100
**当前盈亏**: {result.weakest_position.pnl_pct:+.1f}%
**可替换性**: {result.weakest_position.replaceability}

"""

    alert += f"""**决策理由**: {result.reason}

**建议动作**: {result.recommended_action}
"""

    if result.risk_warning:
        alert += f"\n⚠️ **风险提示**: {result.risk_warning}\n"

    alert += "\n⚡ 仓位冲突决策引擎 · 自动评分 · 仅供参考不构成投资建议"

    return alert


# ============================================================
# 全局单例
# ============================================================

_resolver = None

def get_resolver(max_positions: int = 12, max_replace_per_day: int = 2) -> PositionConflictResolver:
    """获取全局决策引擎单例"""
    global _resolver
    if _resolver is None:
        _resolver = PositionConflictResolver(max_positions, max_replace_per_day)
    return _resolver


def reset_resolver():
    """重置决策引擎 (测试用)"""
    global _resolver
    _resolver = None
