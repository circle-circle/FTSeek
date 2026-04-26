"""
Trade Evidence Chain (交易证据链) - FTSeek v6.0

整合设计：基于多模型归因与深度复盘方案
核心目标：建立完整的因果链追踪体系，让每一笔交易都能回答：
1. 这笔交易为什么发生？
2. 由哪个场景触发？
3. 由哪个策略发起？
4. 共识和风控为什么放行？
5. 交易是如何执行的？
6. 盈亏来自哪里？
7. 下一次应该优化哪个环节？

数据层级：
1. Evidence Layer (证据层) - 全链路留痕，可回放、可审计、可解释
2. Attribution Layer (归因层) - 标签化归因，结构化优先于文本化
3. Feedback Layer (反馈层) - 自动反哺策略权重、场景识别、风控模板
"""
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple
from enum import Enum
import json

logger = logging.getLogger("ftseek.evidence.chain")
logger.propagate = False


# ==================== 交易时段定义 (四维分析) ====================

# 港股交易时段划分
TRADING_SESSIONS = {
    "pre_market": ("09:00", "09:30"),    # 盘前(跨市场分析期)
    "opening": ("09:30", "10:00"),       # 开盘(跨市场执行期)
    "morning": ("10:00", "12:00"),       # 上午(趋势策略期)
    "afternoon": ("13:00", "15:00"),     # 下午
    "closing": ("15:00", "16:00"),       # 尾盘
}


def detect_trading_session(timestamp: str) -> str:
    """
    根据时间戳检测交易时段
    
    Args:
        timestamp: 时间戳字符串 (格式: "HH:MM" 或完整时间)
    
    Returns:
        时段名称 (pre_market/opening/morning/afternoon/closing/unknown)
    """
    try:
        # 提取时间部分
        if "T" in timestamp:
            # ISO格式: "2026-04-12T09:15:00"
            time_part = timestamp.split("T")[1][:5]
        elif " " in timestamp:
            # 空格分隔: "2026-04-12 09:35:00"
            time_part = timestamp.split(" ")[1][:5]
        else:
            # 仅时间: "09:35:00"
            time_part = timestamp[:5]
        
        hour, minute = map(int, time_part.split(":"))
        time_minutes = hour * 60 + minute
        
        for session_name, (start, end) in TRADING_SESSIONS.items():
            start_h, start_m = map(int, start.split(":"))
            end_h, end_m = map(int, end.split(":"))
            start_minutes = start_h * 60 + start_m
            end_minutes = end_h * 60 + end_m
            
            if start_minutes <= time_minutes < end_minutes:
                return session_name
        
        return ""  # 非交易时段返回空字符串
    
    except Exception as e:
        logger.debug(f"交易时段检测失败: {e}")
        return ""


# ==================== 归因标签体系 (Attribution Tags) ====================

class ResultTag(str, Enum):
    """结果归因标签"""
    WIN = "win"                      # 盈利交易
    LOSS = "loss"                    # 亏损交易
    BREAK_EVEN = "break_even"        # 保本交易
    SMALL_WIN = "small_win"          # 小盈 (<1%)
    SMALL_LOSS = "small_loss"        # 小亏 (<1%)
    BIG_WIN = "big_win"              # 大盈 (>3%)
    BIG_LOSS = "big_loss"            # 大亏 (>3%)


class DirectionTag(str, Enum):
    """方向归因标签"""
    DIRECTION_CORRECT = "direction_correct"       # 方向判断正确
    DIRECTION_WRONG = "direction_wrong"           # 方向判断错误
    MARKET_FLAT = "market_flat"                   # 市场走平


class SceneTag(str, Enum):
    """场景归因标签"""
    SCENE_CORRECT = "scene_correct"               # 场景识别正确
    SCENE_MISMATCH = "scene_mismatch"             # 场景误判
    SCENE_LATE_SWITCH = "scene_late_switch"       # 场景切换过晚
    SCENE_EARLY_SWITCH = "scene_early_switch"     # 场景切换过早
    SCENE_GRAY_ZONE = "scene_gray_zone"           # 灰区场景


class StrategyTag(str, Enum):
    """策略归因标签"""
    STRATEGY_FIT = "strategy_fit"                 # 策略适配场景
    STRATEGY_MISFIT = "strategy_misfit"           # 策略不适配
    STRATEGY_OVERWEIGHT = "strategy_overweight"   # 策略权重过高
    STRATEGY_UNDERWEIGHT = "strategy_underweight" # 策略权重过低


class SignalTag(str, Enum):
    """信号归因标签"""
    SIGNAL_HIGH_NOISE = "signal_high_noise"       # 高噪声信号
    SIGNAL_CONFLICT = "signal_conflict"           # 信号冲突
    SIGNAL_WEAK = "signal_weak"                   # 弱信号
    SIGNAL_STRONG = "signal_strong"               # 强信号
    SIGNAL_FALSE_BREAKOUT = "signal_false_breakout"  # 假突破


class ExecutionTag(str, Enum):
    """执行归因标签"""
    EXECUTION_GOOD = "execution_good"             # 执行良好
    SLIPPAGE_HIGH = "slippage_high"               # 高滑点
    PARTIAL_FILL = "partial_fill"                 # 部分成交
    LATE_ENTRY = "late_entry"                     # 入场过晚
    EARLY_ENTRY = "early_entry"                   # 入场过早
    DELAY_HIGH = "delay_high"                     # 延迟过高


class RiskTag(str, Enum):
    """风控归因标签"""
    STOP_LOSS_HIT = "stop_loss_hit"               # 止损触发
    TAKE_PROFIT_HIT = "take_profit_hit"           # 止盈触发
    TRAILING_STOP_HIT = "trailing_stop_hit"       # 移动止损触发
    RISK_CUT = "risk_cut"                         # 风控强平
    POSITION_TOO_LARGE = "position_too_large"     # 仓位过大
    POSITION_TOO_SMALL = "position_too_small"     # 仓位过小
    STOP_TOO_TIGHT = "stop_too_tight"             # 止损过紧
    STOP_TOO_LOOSE = "stop_too_loose"             # 止损过松


class TimingTag(str, Enum):
    """时机归因标签"""
    TIMING_EXCELLENT = "timing_excellent"         # 时机优秀
    TIMING_GOOD = "timing_good"                   # 时机良好
    TIMING_FAIR = "timing_fair"                   # 时机一般
    TIMING_POOR = "timing_poor"                   # 时机较差
    EARLY_EXIT = "early_exit"                     # 出场过早
    LATE_EXIT = "late_exit"                       # 出场过晚
    MISSED_REVERSAL = "missed_reversal"           # 错过反转


class NewsTag(str, Enum):
    """新闻归因标签"""
    NEWS_FADE = "news_fade"                       # 新闻效应消退
    NEWS_DRIVEN = "news_driven"                   # 新闻驱动
    FALSE_NEWS = "false_news"                     # 虚假新闻


class MarketTag(str, Enum):
    """市场归因标签"""
    TREND_CONTINUATION = "trend_continuation"     # 趋势延续
    REVERSAL = "reversal"                         # 趋势反转
    ARBITRAGE_WINDOW = "arbitrage_window"         # 套利窗口
    ARBITRAGE_EXPIRED = "arbitrage_expired"       # 套利过期
    RISK_AVOIDANCE = "risk_avoidance"             # 风险规避


# ==================== 证据层数据模型 ====================

@dataclass
class EvidenceLayer:
    """
    证据层数据 - 全链路留痕

    记录从场景识别到交易结束的完整证据链
    """
    # 基础标识
    trace_id: str = ""
    session_id: str = ""                    # 交易会话 ID
    symbol: str = ""                        # 股票代码
    timestamp_open: str = ""                # 开仓时间戳
    timestamp_close: str = ""               # 平仓时间戳

    # 策略与场景
    strategy_mode: str = ""                 # 策略模式 (confluence_v2/sentiment_ai/etc)
    primary_scene: str = ""                 # 主场景 (TREND_STRONG/RANGE_BOUND/etc)
    secondary_scenes: List[str] = field(default_factory=list)  # 次要场景列表
    primary_strategy: str = ""              # 主策略
    secondary_strategies: List[str] = field(default_factory=list)  # 次要策略

    # 决策信息
    signal_sources: List[str] = field(default_factory=list)  # 信号来源
    consensus_score: float = 0.0            # 共识分数
    confidence: float = 0.0                 # 置信度
    scene_confidence: Dict[str, float] = field(default_factory=dict)  # 各场景置信度
    risk_decision: str = ""                 # 风控决策 (pass/reject/modify)
    approval_status: str = ""               # 审批状态 (approved/pending/rejected)

    # LLM 推理记录
    llm_reasoning: str = ""                 # LLM 推理过程
    llm_confidence: float = 0.0             # LLM 置信度
    llm_key_factors: List[str] = field(default_factory=list)  # LLM 关键因子

    # 市场状态快照
    regime_snapshot: Dict[str, Any] = field(default_factory=dict)  # 市场状态快照
    z_score: float = 0.0                    # Z-Score (标准分)
    news_sentiment: float = 0.0             # 新闻情绪分
    volatility_snapshot: float = 0.0        # 波动率快照

    # 仲裁日志
    conflict_resolution_log: List[Dict] = field(default_factory=list)  # 冲突解决日志
    
    # 【优化1 2026-04-12】时段维度 (四维分析扩展)
    trading_session: str = ""               # 交易时段 (pre_market/opening/morning/afternoon/closing)
    session_start_time: str = ""            # 时段开始时间
    session_duration_minutes: int = 0       # 时段持续分钟数

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict) -> 'EvidenceLayer':
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class AttributionLayer:
    """
    归因层数据 - 标签化归因

    结构化标签优先于文本化描述
    """
    # 结果归因
    result_tags: List[str] = field(default_factory=list)  # 结果标签
    pnl_category: str = ""                  # 盈亏分类 (win/loss/break_even/etc)

    # 方向归因
    direction_tags: List[str] = field(default_factory=list)  # 方向标签
    direction_correct: bool = False         # 方向是否正确

    # 场景归因
    scene_tags: List[str] = field(default_factory=list)  # 场景标签
    scene_correct: bool = False             # 场景是否正确

    # 策略归因
    strategy_tags: List[str] = field(default_factory=list)  # 策略标签
    strategy_fit: bool = False              # 策略是否适配

    # 信号归因
    signal_tags: List[str] = field(default_factory=list)  # 信号标签
    signal_quality: str = ""                # 信号质量 (high/medium/low)

    # 执行归因
    execution_tags: List[str] = field(default_factory=list)  # 执行标签
    execution_quality: str = ""             # 执行质量 (excellent/good/fair/poor)
    slippage_category: str = ""             # 滑点分类 (low/medium/high)
    slippage_bps: float = 0.0               # 滑点基点数

    # 风控归因
    risk_tags: List[str] = field(default_factory=list)  # 风控标签
    risk_quality: str = ""                  # 风控质量 (good/fair/poor)
    position_quality: str = ""              # 仓位质量 (optimal/too_large/too_small)

    # 时机归因
    timing_tags: List[str] = field(default_factory=list)  # 时机标签
    timing_quality: str = ""                # 时机质量 (excellent/good/fair/poor)
    entry_timing: str = ""                  # 入场时机 (early/optimal/late)
    exit_timing: str = ""                   # 出场时机 (early/optimal/late)

    # 归因评分 (0-100)
    direction_score: int = 0                # 方向评分
    scene_score: int = 0                    # 场景评分
    strategy_score: int = 0                 # 策略评分
    signal_score: int = 0                   # 信号评分
    execution_score: int = 0                # 执行评分
    timing_score: int = 0                   # 时机评分
    risk_score: int = 0                     # 风控评分
    overall_score: int = 0                  # 综合评分

    # 主要原因 (结构化)
    success_factors: List[str] = field(default_factory=list)  # 成功因素
    failure_factors: List[str] = field(default_factory=list)  # 失败因素

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict) -> 'AttributionLayer':
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class FeedbackLayer:
    """
    反馈层数据 - 自动反哺

    归因结果反哺到策略、场景、风控、执行层
    """
    # 策略层反馈
    strategy_weight_adjustment: float = 0.0  # 策略权重调整建议
    strategy_enabled: bool = True           # 策略是否应启用
    strategy_scenes_to_avoid: List[str] = field(default_factory=list)  # 应避免的场景
    strategy_scenes_to_favor: List[str] = field(default_factory=list)  # 应偏好的场景

    # 场景层反馈
    scene_confidence_adjustment: Dict[str, float] = field(default_factory=dict)  # 场景置信度调整
    scene_threshold_adjustment: Dict[str, float] = field(default_factory=dict)  # 场景阈值调整
    scene_gray_zones: List[str] = field(default_factory=list)  # 灰区场景

    # 风控层反馈
    stop_loss_adjustment: float = 0.0       # 止损调整建议
    take_profit_adjustment: float = 0.0     # 止盈调整建议
    position_size_adjustment: float = 0.0   # 仓位调整建议
    risk_rules_to_add: List[str] = field(default_factory=list)  # 应添加的风控规则

    # 执行层反馈
    execution_rules_to_update: List[str] = field(default_factory=list)  # 应更新的执行规则
    order_type_suggestion: str = ""         # 订单类型建议 (market/limit/twap/iceberg)
    slippage_tolerance_adjustment: float = 0.0  # 滑点容忍度调整

    # 自动优化建议
    auto_optimization_suggestions: List[Dict] = field(default_factory=list)  # 自动优化建议

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict) -> 'FeedbackLayer':
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


# ==================== Trade Evidence Chain ====================

@dataclass
class TradeEvidenceChain:
    """
    交易证据链 - 整合 Evidence/Attribution/Feedback 三层

    使用示例:
        chain = TradeEvidenceChain(
            trace_id="TRC-20260412-000123",
            symbol="00700.HK",
            primary_scene="GAP_EVENT",
            strategy_mode="cross_market_arb"
        )

        # 填充证据层
        chain.evidence.consensus_score = 0.85
        chain.evidence.llm_reasoning = "美股昨夜受利好驱动..."

        # 平仓后填充归因层
        chain.compute_attribution(
            pnl_percent=2.9,
            slippage_bps=1.5,
            entry_timing_score=75
        )

        # 生成反馈
        feedback = chain.generate_feedback()
    """

    # 基础信息
    trace_id: str = ""
    symbol: str = ""
    created_at: str = ""
    updated_at: str = ""

    # 三层数据
    evidence: EvidenceLayer = field(default_factory=EvidenceLayer)
    attribution: AttributionLayer = field(default_factory=AttributionLayer)
    feedback: FeedbackLayer = field(default_factory=FeedbackLayer)

    # 执行结果 (与 ResultLayer 对应)
    pnl: float = 0.0
    pnl_percent: float = 0.0
    exit_reason: str = ""
    holding_period_minutes: int = 0

    # 状态
    is_closed: bool = False
    status: str = "open"  # open/closed/analysed

    def to_dict(self) -> Dict:
        return {
            "trace_id": self.trace_id,
            "symbol": self.symbol,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "is_closed": self.is_closed,
            "status": self.status,
            "pnl": self.pnl,
            "pnl_percent": self.pnl_percent,
            "exit_reason": self.exit_reason,
            "holding_period_minutes": self.holding_period_minutes,
            "evidence": self.evidence.to_dict(),
            "attribution": self.attribution.to_dict(),
            "feedback": self.feedback.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Dict) -> 'TradeEvidenceChain':
        chain = cls()
        chain.trace_id = data.get('trace_id', '')
        chain.symbol = data.get('symbol', '')
        chain.created_at = data.get('created_at', '')
        chain.updated_at = data.get('updated_at', '')
        chain.is_closed = data.get('is_closed', False)
        chain.status = data.get('status', 'open')
        chain.pnl = data.get('pnl', 0.0)
        chain.pnl_percent = data.get('pnl_percent', 0.0)
        chain.exit_reason = data.get('exit_reason', '')
        chain.holding_period_minutes = data.get('holding_period_minutes', 0)

        if 'evidence' in data:
            chain.evidence = EvidenceLayer.from_dict(data['evidence'])
        if 'attribution' in data:
            chain.attribution = AttributionLayer.from_dict(data['attribution'])
        if 'feedback' in data:
            chain.feedback = FeedbackLayer.from_dict(data['feedback'])

        return chain

    def compute_attribution(self, pnl_percent: float, slippage_bps: float,
                           entry_timing_score: int, exit_timing_score: int,
                           max_profit_pct: float, max_loss_pct: float) -> AttributionLayer:
        """
        计算归因标签

        Args:
            pnl_percent: 盈亏百分比
            slippage_bps: 滑点 (基点)
            entry_timing_score: 入场时机评分 (0-100)
            exit_timing_score: 出场时机评分 (0-100)
            max_profit_pct: 最大浮盈百分比
            max_loss_pct: 最大浮亏百分比
        """
        self.pnl_percent = pnl_percent

        # === 结果归因 ===
        self.attribution.pnl_category = self._get_result_tag(pnl_percent)
        self.attribution.result_tags = [self.attribution.pnl_category]

        # === 方向归因 ===
        # 简化逻辑：假设 direction 由 evidence.strategy_mode 决定
        if pnl_percent > 0:
            self.attribution.direction_tags.append(DirectionTag.DIRECTION_CORRECT.value)
            self.attribution.direction_correct = True
        else:
            self.attribution.direction_tags.append(DirectionTag.DIRECTION_WRONG.value)
            self.attribution.direction_correct = False

        # === 执行归因 ===
        if slippage_bps > 10:
            self.attribution.execution_tags.append(ExecutionTag.SLIPPAGE_HIGH.value)
            self.attribution.execution_quality = "poor"
        elif slippage_bps > 5:
            self.attribution.execution_quality = "fair"
        else:
            self.attribution.execution_tags.append(ExecutionTag.EXECUTION_GOOD.value)
            self.attribution.execution_quality = "good"

        # === 时机归因 ===
        avg_timing = (entry_timing_score + exit_timing_score) / 2
        if avg_timing >= 80:
            self.attribution.timing_tags.append(TimingTag.TIMING_EXCELLENT.value)
            self.attribution.timing_quality = "excellent"
        elif avg_timing >= 60:
            self.attribution.timing_tags.append(TimingTag.TIMING_GOOD.value)
            self.attribution.timing_quality = "good"
        elif avg_timing >= 40:
            self.attribution.timing_tags.append(TimingTag.TIMING_FAIR.value)
            self.attribution.timing_quality = "fair"
        else:
            self.attribution.timing_tags.append(TimingTag.TIMING_POOR.value)
            self.attribution.timing_quality = "poor"

        if entry_timing_score < 50:
            self.attribution.timing_tags.append(TimingTag.LATE_ENTRY.value)
            self.attribution.entry_timing = "late"
        elif entry_timing_score > 80:
            self.attribution.entry_timing = "early"
        else:
            self.attribution.entry_timing = "optimal"

        # === 风控归因 ===
        if self.exit_reason == "stop_loss":
            self.attribution.risk_tags.append(RiskTag.STOP_LOSS_HIT.value)
        elif self.exit_reason == "take_profit":
            self.attribution.risk_tags.append(RiskTag.TAKE_PROFIT_HIT.value)
        elif self.exit_reason == "trailing_stop":
            self.attribution.risk_tags.append(RiskTag.TRAILING_STOP_HIT.value)

        # 检查仓位质量
        if pnl_percent < -3:
            self.attribution.risk_tags.append(RiskTag.POSITION_TOO_LARGE.value)
            self.attribution.position_quality = "too_large"
        elif pnl_percent > 3:
            self.attribution.risk_tags.append(RiskTag.POSITION_TOO_SMALL.value)
            self.attribution.position_quality = "too_small"
        else:
            self.attribution.position_quality = "optimal"

        # === 计算归因评分 ===
        self.attribution.direction_score = 100 if self.attribution.direction_correct else 0
        self.attribution.execution_score = min(100, max(0, 100 - slippage_bps * 5))
        self.attribution.timing_score = avg_timing
        self.attribution.risk_score = self._compute_risk_score(max_profit_pct, max_loss_pct, pnl_percent)
        self.attribution.overall_score = int(
            self.attribution.direction_score * 0.3 +
            self.attribution.execution_score * 0.2 +
            self.attribution.timing_score * 0.3 +
            self.attribution.risk_score * 0.2
        )

        # === 成功/失败因素 ===
        if pnl_percent > 0:
            if self.attribution.direction_correct:
                self.attribution.success_factors.append("方向判断正确")
            if self.attribution.execution_quality in ["good", "excellent"]:
                self.attribution.success_factors.append("执行良好")
            if self.attribution.timing_quality in ["good", "excellent"]:
                self.attribution.success_factors.append("时机把握准确")
        else:
            if not self.attribution.direction_correct:
                self.attribution.failure_factors.append("方向判断错误")
            if self.attribution.execution_quality == "poor":
                self.attribution.failure_factors.append("执行滑点过高")
            if self.attribution.timing_quality == "poor":
                self.attribution.failure_factors.append("时机把握不佳")

        return self.attribution

    def _get_result_tag(self, pnl_percent: float) -> str:
        """根据盈亏百分比获取结果标签"""
        if pnl_percent >= 3:
            return ResultTag.BIG_WIN.value
        elif pnl_percent >= 1:
            return ResultTag.WIN.value
        elif pnl_percent >= 0:
            return ResultTag.SMALL_WIN.value
        elif pnl_percent >= -1:
            return ResultTag.SMALL_LOSS.value
        elif pnl_percent >= -3:
            return ResultTag.LOSS.value
        else:
            return ResultTag.BIG_LOSS.value

    def _compute_risk_score(self, max_profit_pct: float, max_loss_pct: float,
                           pnl_percent: float) -> int:
        """计算风控评分"""
        score = 50  # 基础分

        # 止损有效性
        if max_loss_pct > -2:
            score += 20  # 回撤控制良好
        elif max_loss_pct < -5:
            score -= 20  # 回撤过大

        # 盈利捕获
        if max_profit_pct > 0:
            capture_rate = max(0, pnl_percent) / max_profit_pct
            score += int(capture_rate * 30)

        return min(100, max(0, score))

    def generate_feedback(self) -> FeedbackLayer:
        """
        生成反馈建议

        基于归因结果，生成策略、场景、风控、执行层的优化建议
        """
        # === 策略层反馈 ===
        if self.attribution.overall_score >= 80:
            self.feedback.strategy_weight_adjustment = 0.1  # 增加权重 10%
            self.feedback.strategy_enabled = True
        elif self.attribution.overall_score <= 40:
            self.feedback.strategy_weight_adjustment = -0.2  # 降低权重 20%
            if self.attribution.overall_score < 20:
                self.feedback.strategy_scenes_to_avoid.append(self.evidence.primary_scene)

        # === 场景层反馈 ===
        if self.attribution.scene_correct:
            self.feedback.scene_confidence_adjustment[self.evidence.primary_scene] = 0.05
        else:
            self.feedback.scene_confidence_adjustment[self.evidence.primary_scene] = -0.1
            self.feedback.scene_gray_zones.append(self.evidence.primary_scene)

        # === 风控层反馈 ===
        if RiskTag.STOP_LOSS_HIT.value in self.attribution.risk_tags:
            if self.attribution.pnl_category in [ResultTag.LOSS.value, ResultTag.BIG_LOSS.value]:
                self.feedback.risk_rules_to_add.append("考虑放宽止损或优化入场时机")

        if RiskTag.POSITION_TOO_LARGE.value in self.attribution.risk_tags:
            self.feedback.position_size_adjustment = -0.1  # 降低仓位 10%
        elif RiskTag.POSITION_TOO_SMALL.value in self.attribution.risk_tags:
            self.feedback.position_size_adjustment = 0.1  # 增加仓位 10%

        # === 执行层反馈 ===
        if ExecutionTag.SLIPPAGE_HIGH.value in self.attribution.execution_tags:
            self.feedback.execution_rules_to_update.append("考虑使用限价单或 TWAP 算法")
            self.feedback.order_type_suggestion = "limit"

        # === 自动优化建议 ===
        self.feedback.auto_optimization_suggestions = self._generate_auto_suggestions()

        return self.feedback

    def _generate_auto_suggestions(self) -> List[Dict]:
        """生成自动优化建议"""
        suggestions = []

        # 基于失败因素生成建议
        for factor in self.attribution.failure_factors:
            if factor == "方向判断错误":
                suggestions.append({
                    "type": "strategy",
                    "priority": "high",
                    "suggestion": f"优化 {self.evidence.strategy_mode} 策略的方向判断逻辑",
                    "action": "review_signal_threshold"
                })
            elif factor == "执行滑点过高":
                suggestions.append({
                    "type": "execution",
                    "priority": "medium",
                    "suggestion": "优化订单执行策略，考虑使用限价单",
                    "action": "switch_to_limit_order"
                })
            elif factor == "时机把握不佳":
                suggestions.append({
                    "type": "timing",
                    "priority": "medium",
                    "suggestion": "优化入场时机判断，等待更强信号确认",
                    "action": "increase_entry_threshold"
                })

        return suggestions

    def generate_summary(self) -> str:
        """
        生成交易摘要 (自然语言)

        用于钉钉/飞书推送和人工复盘
        
        【优化3 2026-04-12】增强为因果链条式摘要
        """
        # 使用AttributionEngine的因果链条摘要
        try:
            from app.evidence.attribution_engine import AttributionEngine
            return AttributionEngine.generate_causal_summary(self)
        except Exception as e:
            logger.debug(f"因果链条摘要生成失败，使用基础版本: {e}")
            # 降级到基础版本
            return self._generate_basic_summary()
    
    def _generate_basic_summary(self) -> str:
        """
        基础版本摘要 (降级方案)
        """
        lines = []
        lines.append(f"[{self.trace_id}]")
        lines.append(f"场景：{self.evidence.primary_scene} (Confidence: {self.evidence.confidence:.2f})")
        lines.append(f"策略：{self.evidence.strategy_mode}")
        
        if self.evidence.llm_reasoning:
            lines.append(f"LLM 推理：{self.evidence.llm_reasoning[:50]}...")
        
        pnl_status = "盈利" if self.pnl > 0 else "亏损"
        lines.append(f"结果：{pnl_status} {self.pnl_percent:.2f}%")
        
        if self.attribution.success_factors:
            lines.append(f"成功因素：{', '.join(self.attribution.success_factors)}")
        if self.attribution.failure_factors:
            lines.append(f"失败因素：{', '.join(self.attribution.failure_factors)}")
        
        if self.feedback.auto_optimization_suggestions:
            top_suggestion = self.feedback.auto_optimization_suggestions[0]
            lines.append(f"建议：{top_suggestion['suggestion']}")
        
        return "\n".join(lines)


# ==================== 工具函数 ====================

def create_trace_id() -> str:
    """生成唯一的 trace ID"""
    import uuid
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    unique_id = str(uuid.uuid4())[:8]
    return f"TRC-{timestamp}-{unique_id}"


def tag_to_list(tags: List[str]) -> str:
    """将标签列表转换为字符串 (用于数据库存储)"""
    return json.dumps(tags, ensure_ascii=False)


def list_to_tags(tag_str: str) -> List[str]:
    """将字符串转换为标签列表"""
    if not tag_str:
        return []
    try:
        return json.loads(tag_str)
    except Exception:
        return []
