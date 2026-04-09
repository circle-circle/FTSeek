"""
交易全链路记录 (Trade Chain)

记录从信号产生到交易结束的完整链路，支持深度归因分析。

数据层级:
1. 信号层 (Signal) - 信号分数、置信度、共识权重、风控检查
2. 决策层 (Decision) - 决策原因、信心、仓位计划与调整
3. 执行层 (Execution) - 订单时间、预期价、实际成交价、滑点、延迟
4. 结果层 (Result) - 持仓周期、最大浮盈/浮亏、出场原因、净收益
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Any
from enum import Enum

logger = logging.getLogger("ftseek.analytics.trade_chain")
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


# ==================== 数据类定义 ====================

@dataclass
class SignalLayer:
    """信号层数据"""
    signal_id: str = ""
    signal_time: str = ""
    signal_score: float = 0.0
    signal_confidence: float = 0.0
    consensus_passed: bool = False
    consensus_weights: Dict[str, float] = field(default_factory=dict)
    risk_check_passed: bool = False
    risk_reason: str = ""
    market_regime: str = ""           # 市场状态
    volatility_atr: float = 0.0       # ATR 波动率
    ai_sentiment_score: float = 0.0   # AI 情绪分

    def to_dict(self) -> Dict:
        return {
            "signal_id": self.signal_id,
            "signal_time": self.signal_time,
            "signal_score": self.signal_score,
            "signal_confidence": self.signal_confidence,
            "consensus_passed": self.consensus_passed,
            "consensus_weights": self.consensus_weights,
            "risk_check_passed": self.risk_check_passed,
            "risk_reason": self.risk_reason,
            "market_regime": self.market_regime,
            "volatility_atr": self.volatility_atr,
            "ai_sentiment_score": self.ai_sentiment_score,
        }


@dataclass
class DecisionLayer:
    """决策层数据"""
    decision_reason: str = ""         # technical/sentiment/confluence
    decision_confidence: int = 5      # 1-10
    position_planned: float = 0.0     # 计划仓位 (%)
    position_adjusted: float = 0.0    # 实际仓位 (%)
    position_adjust_reason: str = ""  # 降仓原因
    weight_config_snapshot: Dict = field(default_factory=dict)
    account_state: Dict = field(default_factory=dict)  # 账户状态

    def to_dict(self) -> Dict:
        return {
            "decision_reason": self.decision_reason,
            "decision_confidence": self.decision_confidence,
            "position_planned": self.position_planned,
            "position_adjusted": self.position_adjusted,
            "position_adjust_reason": self.position_adjust_reason,
            "weight_config_snapshot": self.weight_config_snapshot,
            "account_state": self.account_state,
        }


@dataclass
class ExecutionLayer:
    """执行层数据"""
    order_time: str = ""
    expected_price: float = 0.0
    actual_fill_price: float = 0.0
    slippage_bps: float = 0.0         # 滑点 (基点)
    execution_latency_ms: int = 0     # 延迟 (毫秒)
    fill_reason: str = ""             # market/limit/partial
    commission: float = 0.0           # 佣金
    stamp_duty: float = 0.0           # 印花税
    transaction_fee: float = 0.0      # 交易费
    total_cost: float = 0.0           # 总成本

    def to_dict(self) -> Dict:
        return {
            "order_time": self.order_time,
            "expected_price": self.expected_price,
            "actual_fill_price": self.actual_fill_price,
            "slippage_bps": self.slippage_bps,
            "execution_latency_ms": self.execution_latency_ms,
            "fill_reason": self.fill_reason,
            "commission": self.commission,
            "stamp_duty": self.stamp_duty,
            "transaction_fee": self.transaction_fee,
            "total_cost": self.total_cost,
        }


@dataclass
class ResultLayer:
    """结果层数据"""
    exit_time: str = ""
    exit_price: float = 0.0
    exit_reason: str = ""
    holding_period_minutes: int = 0
    max_profit_during: float = 0.0    # 最大浮盈
    max_loss_during: float = 0.0      # 最大浮亏
    pnl: float = 0.0
    pnl_after_fee: float = 0.0        # 手续费后净收益
    pnl_percent: float = 0.0
    risk_reward_actual: float = 0.0   # 实际盈亏比
    max_drawdown_during: float = 0.0  # 持仓期间最大回撤

    def to_dict(self) -> Dict:
        return {
            "exit_time": self.exit_time,
            "exit_price": self.exit_price,
            "exit_reason": self.exit_reason,
            "holding_period_minutes": self.holding_period_minutes,
            "max_profit_during": self.max_profit_during,
            "max_loss_during": self.max_loss_during,
            "pnl": self.pnl,
            "pnl_after_fee": self.pnl_after_fee,
            "pnl_percent": self.pnl_percent,
            "risk_reward_actual": self.risk_reward_actual,
            "max_drawdown_during": self.max_drawdown_during,
        }


@dataclass
class TradeChain:
    """
    交易全链路记录

    完整记录一笔交易从信号产生到最终结果的全过程
    """
    trade_id: str = ""
    code: str = ""
    created_at: str = ""
    updated_at: str = ""

    # 四层数据
    signal: SignalLayer = field(default_factory=SignalLayer)
    decision: DecisionLayer = field(default_factory=DecisionLayer)
    execution: ExecutionLayer = field(default_factory=ExecutionLayer)
    result: Optional[ResultLayer] = None

    # 衍生指标 (用于快速查询)
    is_closed: bool = False
    is_profitable: bool = False
    days_ago: int = 0

    def to_dict(self) -> Dict:
        return {
            "trade_id": self.trade_id,
            "code": self.code,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "is_closed": self.is_closed,
            "is_profitable": self.is_profitable,
            "days_ago": self.days_ago,
            "signal": self.signal.to_dict(),
            "decision": self.decision.to_dict(),
            "execution": self.execution.to_dict(),
            "result": self.result.to_dict() if self.result else None,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> 'TradeChain':
        """从字典创建"""
        chain = cls()
        chain.trade_id = data.get('trade_id', '')
        chain.code = data.get('code', '')
        chain.created_at = data.get('created_at', '')
        chain.updated_at = data.get('updated_at', '')
        chain.is_closed = data.get('is_closed', False)
        chain.is_profitable = data.get('is_profitable', False)
        chain.days_ago = data.get('days_ago', 0)

        signal_data = data.get('signal', {})
        if signal_data:
            chain.signal = SignalLayer(**signal_data)

        decision_data = data.get('decision', {})
        if decision_data:
            chain.decision = DecisionLayer(**decision_data)

        execution_data = data.get('execution', {})
        if execution_data:
            chain.execution = ExecutionLayer(**execution_data)

        result_data = data.get('result')
        if result_data:
            chain.result = ResultLayer(**result_data)

        return chain

    def complete_trade(self, exit_price: float, exit_reason: str,
                       pnl: float, pnl_percent: float,
                       holding_minutes: int, max_profit: float,
                       max_loss: float, total_cost: float) -> None:
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
            max_profit_during=max_profit,
            max_loss_during=max_loss,
            pnl=pnl,
            pnl_after_fee=pnl - total_cost,
            pnl_percent=pnl_percent,
            risk_reward_actual=abs(pnl) / total_cost if total_cost > 0 else 0,
            max_drawdown_during=max_loss,
        )

        self.is_closed = True
        self.is_profitable = pnl > 0
        self.updated_at = datetime.now().isoformat()

    def update_execution(self, actual_fill_price: float,
                         slippage_bps: float,
                         latency_ms: int,
                         cost_breakdown: Dict) -> None:
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
        # 买入时，实际成交价高于预期价为正滑点
        slippage = (actual - expected) / expected * 10000
    else:
        # 卖出时，实际成交价低于预期价为正滑点
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
