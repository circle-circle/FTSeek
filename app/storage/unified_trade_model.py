"""
Unified Trade Record (UTR) - 统一交易数据模型

整合 TradeChain 四层架构 + TradeRecord 复盘字段 + TradeEvaluation 评估字段
支持单一数据库存储，消除数据冗余

数据层级:
1. Signal Layer (信号层) - 信号分数、置信度、共识权重、风控检查
2. Decision Layer (决策层) - 决策原因、信心、仓位计划与调整、心理状态
3. Execution Layer (执行层) - 订单时间、预期价、实际成交价、滑点、延迟
4. Result Layer (结果层) - 持仓周期、最大浮盈/浮亏、出场原因、净收益、评估评分
"""
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Dict, List, Optional, Any
from enum import Enum

logger = logging.getLogger("ftseek.storage.unified_model")
logger.propagate = False


# ==================== 枚举定义 ====================

class DecisionReason(str, Enum):
    """决策原因"""
    TECHNICAL = "technical"           # 技术分析
    FUNDAMENTAL = "fundamental"       # 基本面
    SENTIMENT = "sentiment"           # 情绪分析
    NEWS = "news"                     # 新闻驱动
    CONFLUENCE = "confluence"         # 多因子共振
    IMPULSE = "impulse"               # 冲动交易
    AUTO_EXIT = "auto_exit"           # 自动退出


class ExitReason(str, Enum):
    """出场原因"""
    TAKE_PROFIT = "take_profit"       # 止盈
    STOP_LOSS = "stop_loss"           # 止损
    SIGNAL_REVERSE = "signal_reverse" # 信号反转
    TIMEOUT = "timeout"               # 超时
    MANUAL = "manual"                 # 手动平仓
    RISK_CUT = "risk_cut"             # 风控强平


class FillReason(str, Enum):
    """成交原因"""
    MARKET = "market"                 # 市价单
    LIMIT = "limit"                   # 限价单
    PARTIAL = "partial"               # 部分成交
    REJECTED = "rejected"             # 被拒绝


class EmotionState(str, Enum):
    """心理状态"""
    CALM = "calm"                     # 平静
    ANXIOUS = "anxious"               # 焦虑
    EXCITED = "excited"               # 兴奋
    FEARFUL = "fearful"               # 恐惧
    GREEDY = "greedy"                 # 贪婪
    FRUSTRATED = "frustrated"         # 沮丧


class DecisionQuality(str, Enum):
    """决策质量等级"""
    EXCELLENT = "excellent"           # 优秀
    GOOD = "good"                     # 良好
    FAIR = "fair"                     # 一般
    POOR = "poor"                     # 较差
    BAD = "bad"                       # 差


# ==================== 四层数据模型 ====================

@dataclass
class SignalLayer:
    """
    信号层数据

    记录交易信号产生时的所有信息
    """
    signal_id: str = ""
    signal_time: str = ""
    signal_score: float = 0.0         # 信号分数 0-1
    signal_confidence: float = 0.0    # 信号置信度 0-1
    consensus_passed: bool = False
    consensus_weights: Dict[str, float] = field(default_factory=dict)
    risk_check_passed: bool = False
    risk_reason: str = ""
    market_regime: str = ""           # 市场状态 (trending/ranging/neutral)
    volatility_atr: float = 0.0       # ATR 波动率
    ai_sentiment_score: float = 0.0   # AI 情绪分

    # 扩展字段 (来自 TradeRecord)
    ai_score: float = 0.0
    factor_scores: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict) -> 'SignalLayer':
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class DecisionLayer:
    """
    决策层数据

    记录交易决策时的心理状态和仓位决策
    """
    # 核心决策字段
    decision_reason: str = ""         # technical/sentiment/confluence/news/impulse
    decision_confidence: int = 5      # 1-10
    position_planned: float = 0.0     # 计划仓位 (%)
    position_adjusted: float = 0.0    # 实际仓位 (%)
    position_adjust_reason: str = ""  # 降仓原因

    # 配置快照
    weight_config_snapshot: Dict = field(default_factory=dict)
    account_state: Dict = field(default_factory=dict)

    # 心理复盘字段 (来自 TradeRecord)
    emotion_state: str = "calm"       # calm/anxious/excited/fearful/greedy/frustrated
    emotion_intensity: int = 3        # 1-10 情绪强度
    confidence_entry: int = 5         # 入场时信心 1-10

    # 决策复盘字段
    strategy_type: str = ""           # confluence_v2/fusion_v3/etc
    decision_time_spent: int = 0      # 决策耗时 (秒)
    decision_quality_score: int = 5   # 1-10 决策质量评分 (事后评分)
    decision_notes: str = ""

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict) -> 'DecisionLayer':
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class ExecutionLayer:
    """
    执行层数据

    记录交易执行的详细信息
    """
    order_time: str = ""
    expected_price: float = 0.0
    actual_fill_price: float = 0.0
    slippage_bps: float = 0.0         # 滑点 (基点)
    execution_latency_ms: int = 0     # 延迟 (毫秒)
    fill_reason: str = ""             # market/limit/partial/rejected
    commission: float = 0.0           # 佣金
    stamp_duty: float = 0.0           # 印花税
    transaction_fee: float = 0.0      # 交易费
    total_cost: float = 0.0           # 总成本

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict) -> 'ExecutionLayer':
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class ResultLayer:
    """
    结果层数据

    记录交易结果和事后评估
    """
    # 基础结果
    exit_time: str = ""
    exit_price: float = 0.0
    exit_reason: str = ""
    holding_period_minutes: int = 0
    holding_period_days: int = 0

    # 盈亏
    pnl: float = 0.0
    pnl_after_fee: float = 0.0        # 手续费后净收益
    pnl_percent: float = 0.0
    total_cost: float = 0.0

    # 极值追踪 (来自 TradeEvaluation)
    max_profit_during: float = 0.0    # 最大浮盈
    max_loss_during: float = 0.0      # 最大浮亏
    price_1min: Optional[float] = None
    price_5min: Optional[float] = None
    price_15min: Optional[float] = None
    price_1hour: Optional[float] = None
    price_eod: Optional[float] = None
    price_next_day: Optional[float] = None

    # 风险评估
    risk_reward_actual: float = 0.0   # 实际盈亏比
    max_drawdown_during: float = 0.0  # 持仓期间最大回撤

    # 评估评分
    entry_score: float = 0.0          # 入场评分 0-100
    exit_score: float = 0.0           # 出场评分 0-100
    timing_score: float = 0.0         # 时机评分 0-100
    overall_score: float = 0.0        # 综合评分 0-100
    decision_quality_score: int = 5   # 决策质量等级 (事后评分)

    # 平仓后评估
    entry_quality: str = ""           # 入场时机评估 excellent/good/fair/poor/bad
    exit_quality: str = ""            # 出场时机评估
    overall_quality: str = ""         # 综合评估
    analysis: str = ""                # 分析报告
    suggestions: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict) -> 'ResultLayer':
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


# ==================== Unified Trade Record ====================

@dataclass
class UnifiedTradeRecord:
    """
    统一交易记录 (Unified Trade Record - UTR)

    整合 TradeChain 四层架构 + TradeRecord 复盘字段 + TradeEvaluation 评估字段

    使用示例:
        # 创建交易记录
        utr = UnifiedTradeRecord(
            trade_id="T001",
            code="00700.HK",
            action="BUY",
            quantity=100
        )

        # 填充信号层
        utr.signal = SignalLayer(
            signal_id="S001",
            signal_score=0.75,
            signal_confidence=0.82,
            market_regime="trending"
        )

        # 填充决策层
        utr.decision = DecisionLayer(
            decision_reason="confluence",
            decision_confidence=7,
            position_planned=0.1
        )

        # 填充执行层
        utr.execution = ExecutionLayer(
            expected_price=380.5,
            actual_fill_price=380.8,
            slippage_bps=7.8
        )

        # 平仓时填充结果层
        utr.complete_trade(
            exit_price=395.0,
            exit_reason="take_profit",
            pnl=1420.0,
            pnl_percent=3.73,
            holding_minutes=135,
            max_profit=2100.0,
            max_loss=-300.0,
            total_cost=50.0
        )
    """

    # ==================== 基础信息 ====================
    trade_id: str = ""
    code: str = ""
    action: str = ""                # BUY/SELL
    quantity: int = 0
    account_type: str = "real"      # real/simulation
    strategy: str = ""              # 策略名称

    # 时间戳
    created_at: str = ""
    updated_at: str = ""

    # ==================== 四层数据 ====================
    signal: SignalLayer = field(default_factory=SignalLayer)
    decision: DecisionLayer = field(default_factory=DecisionLayer)
    execution: ExecutionLayer = field(default_factory=ExecutionLayer)
    result: Optional[ResultLayer] = None

    # ==================== 衍生指标 (用于快速查询) ====================
    is_closed: bool = False
    is_profitable: bool = False
    days_ago: int = 0

    # ==================== 状态标记 ====================
    status: str = "open"            # open/closed/cancelled

    def to_dict(self) -> Dict:
        """转换为字典 (支持 JSON 序列化)"""
        return {
            "trade_id": self.trade_id,
            "code": self.code,
            "action": self.action,
            "quantity": self.quantity,
            "account_type": self.account_type,
            "strategy": self.strategy,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "is_closed": self.is_closed,
            "is_profitable": self.is_profitable,
            "days_ago": self.days_ago,
            "status": self.status,
            "signal": self.signal.to_dict() if self.signal else {},
            "decision": self.decision.to_dict() if self.decision else {},
            "execution": self.execution.to_dict() if self.execution else {},
            "result": self.result.to_dict() if self.result else None,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> 'UnifiedTradeRecord':
        """从字典创建"""
        utr = cls()

        # 基础字段
        for field_name in ['trade_id', 'code', 'action', 'quantity', 'account_type',
                          'strategy', 'created_at', 'updated_at', 'is_closed',
                          'is_profitable', 'days_ago', 'status']:
            if field_name in data:
                setattr(utr, field_name, data[field_name])

        # 四层数据
        if 'signal' in data and data['signal']:
            utr.signal = SignalLayer.from_dict(data['signal'])

        if 'decision' in data and data['decision']:
            utr.decision = DecisionLayer.from_dict(data['decision'])

        if 'execution' in data and data['execution']:
            utr.execution = ExecutionLayer.from_dict(data['execution'])

        if 'result' in data and data['result']:
            utr.result = ResultLayer.from_dict(data['result'])

        return utr

    def complete_trade(
        self,
        exit_price: float,
        exit_reason: str,
        pnl: float,
        pnl_percent: float,
        holding_minutes: int,
        max_profit: float,
        max_loss: float,
        total_cost: float
    ) -> None:
        """
        完成交易记录 (填充结果层)

        Args:
            exit_price: 出场价
            exit_reason: 出场原因
            pnl: 盈亏
            pnl_percent: 盈亏百分比
            holding_minutes: 持仓分钟数
            max_profit: 最大浮盈
            max_loss: 最大浮亏
            total_cost: 总成本
        """
        self.result = ResultLayer(
            exit_time=datetime.now().isoformat(),
            exit_price=exit_price,
            exit_reason=exit_reason,
            holding_period_minutes=holding_minutes,
            holding_period_days=holding_minutes // (60 * 24),
            pnl=pnl,
            pnl_after_fee=pnl - total_cost,
            pnl_percent=pnl_percent,
            total_cost=total_cost,
            max_profit_during=max_profit,
            max_loss_during=max_loss,
            risk_reward_actual=abs(pnl) / total_cost if total_cost > 0 else 0,
            max_drawdown_during=max_loss,
        )

        self.is_closed = True
        self.is_profitable = pnl > 0
        self.status = "closed"
        self.updated_at = datetime.now().isoformat()

    def update_execution(
        self,
        actual_fill_price: float,
        slippage_bps: float,
        latency_ms: int,
        cost_breakdown: Dict[str, float]
    ) -> None:
        """
        更新执行层数据

        Args:
            actual_fill_price: 实际成交价
            slippage_bps: 滑点 (基点)
            latency_ms: 延迟 (毫秒)
            cost_breakdown: 成本明细
        """
        self.execution.actual_fill_price = actual_fill_price
        self.execution.slippage_bps = slippage_bps
        self.execution.execution_latency_ms = latency_ms
        self.execution.commission = cost_breakdown.get('commission', 0.0)
        self.execution.stamp_duty = cost_breakdown.get('stamp_duty', 0.0)
        self.execution.transaction_fee = cost_breakdown.get('transaction_fee', 0.0)
        self.execution.total_cost = cost_breakdown.get('total_cost', 0.0)
        self.execution.order_time = datetime.now().isoformat()

    def update_decision(
        self,
        decision_reason: str = None,
        decision_confidence: int = None,
        position_planned: float = None,
        emotion_state: str = None,
        decision_notes: str = None
    ) -> None:
        """更新决策层数据"""
        if decision_reason is not None:
            self.decision.decision_reason = decision_reason
        if decision_confidence is not None:
            self.decision.decision_confidence = decision_confidence
        if position_planned is not None:
            self.decision.position_planned = position_planned
        if emotion_state is not None:
            self.decision.emotion_state = emotion_state
        if decision_notes is not None:
            self.decision.decision_notes = decision_notes

        self.updated_at = datetime.now().isoformat()

    def update_signal(
        self,
        signal_score: float = None,
        signal_confidence: float = None,
        market_regime: str = None
    ) -> None:
        """更新信号层数据"""
        if signal_score is not None:
            self.signal.signal_score = signal_score
        if signal_confidence is not None:
            self.signal.signal_confidence = signal_confidence
        if market_regime is not None:
            self.signal.market_regime = market_regime

        self.updated_at = datetime.now().isoformat()

    # ==================== 辅助方法 ====================

    def get_decision_quality(self) -> str:
        """获取决策质量等级"""
        if not self.result:
            return ""

        score = self.result.overall_score
        if score >= 80:
            return DecisionQuality.EXCELLENT.value
        elif score >= 60:
            return DecisionQuality.GOOD.value
        elif score >= 40:
            return DecisionQuality.FAIR.value
        elif score >= 20:
            return DecisionQuality.POOR.value
        else:
            return DecisionQuality.BAD.value

    def get_holding_period_str(self) -> str:
        """获取持仓时长的字符串表示"""
        if not self.result:
            return ""

        minutes = self.result.holding_period_minutes
        if minutes < 60:
            return f"{minutes}分钟"
        elif minutes < 1440:
            return f"{minutes / 60:.1f}小时"
        else:
            return f"{minutes / 1440:.1f}天"

    def get_slippage_str(self) -> str:
        """获取滑点的字符串表示"""
        bps = self.execution.slippage_bps
        if bps < 10:
            return f"{bps}bp"
        else:
            return f"{bps / 100:.2f}%"


# ==================== 工具函数 ====================

def calculate_slippage_bps(expected: float, actual: float, side: str) -> float:
    """
    计算滑点 (基点)

    Args:
        expected: 预期价格
        actual: 实际价格
        side: BUY 或 SELL

    Returns:
        滑点 (基点，1bp=0.01%)
    """
    if expected == 0:
        return 0.0

    if side.upper() == 'BUY':
        slippage = (actual - expected) / expected * 10000
    else:
        slippage = (expected - actual) / expected * 10000

    return round(slippage, 2)


def calculate_holding_minutes(entry_time: str, exit_time: str) -> int:
    """
    计算持仓分钟数

    Args:
        entry_time: 入场时间 (ISO 格式)
        exit_time: 出场时间 (ISO 格式)

    Returns:
        持仓分钟数
    """
    try:
        entry = datetime.fromisoformat(entry_time)
        exit = datetime.fromisoformat(exit_time)
        return int((exit - entry).total_seconds() / 60)
    except Exception:
        return 0


def calculate_decision_quality_score(pnl_percent: float, max_profit: float,
                                     max_loss: float, entry_score: float) -> float:
    """
    计算决策质量评分

    Args:
        pnl_percent: 盈亏百分比
        max_profit: 最大盈利机会
        max_loss: 最大亏损风险
        entry_score: 入场评分

    Returns:
        决策质量评分 0-100
    """
    # 基础分：入场评分占 40%
    base_score = entry_score * 0.4

    # 盈亏分：实际盈亏占 30%
    pnl_score = min(30, max(0, 50 + pnl_percent * 10))

    # 机会捕获分：占 20%
    if max_profit > 0:
        capture_rate = max(0, pnl_percent) / max_profit
        opportunity_score = capture_rate * 20
    else:
        opportunity_score = 10 if pnl_percent >= 0 else 5

    # 风控分：占 10%
    risk_score = 10 if max_loss > -2 else 5

    return round(base_score + pnl_score + opportunity_score + risk_score, 2)


# ==================== 单例工厂 ====================

def create_utr(
    trade_id: str,
    code: str,
    action: str,
    quantity: int,
    strategy: str = "",
    account_type: str = "real"
) -> UnifiedTradeRecord:
    """
    创建统一交易记录的便捷工厂函数

    使用示例:
        utr = create_utr(
            trade_id="T001",
            code="00700.HK",
            action="BUY",
            quantity=100,
            strategy="confluence_v2"
        )
    """
    return UnifiedTradeRecord(
        trade_id=trade_id,
        code=code,
        action=action,
        quantity=quantity,
        strategy=strategy,
        account_type=account_type,
        created_at=datetime.now().isoformat(),
        updated_at=datetime.now().isoformat()
    )
