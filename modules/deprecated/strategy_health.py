"""
策略健康度评分 (Strategy Health Scoring)

基于 GPT 分析建议实现的策略状态追踪系统，为每个策略维护：
- 最近 20 笔胜率
- 平均盈亏比
- 最大连续亏损
- 当前市场适配度
- 最近信号频率

用于动态调整策略权重，避免单一策略长期主导错误决策
"""
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from collections import deque
import json

from app import config

logger = logging.getLogger("ftseek.analytics.strategy_health")
logger.propagate = False


@dataclass
class StrategyHealthMetrics:
    """策略健康度指标"""
    strategy_name: str

    # 核心指标
    recent_trades: int = 0          # 最近交易笔数
    recent_win_rate: float = 0.0    # 最近 20 笔胜率
    avg_profit_loss_ratio: float = 0.0  # 平均盈亏比
    max_consecutive_losses: int = 0     # 最大连续亏损
    current_consecutive_losses: int = 0 # 当前连续亏损

    # 市场适配度
    regime_fit_score: float = 0.0   # 当前市场适配度 (0-1)
    current_regime: str = ""        # 当前市场状态

    # 信号特征
    signal_frequency: float = 0.0   # 最近信号频率 ( signals/hour)
    signal_trend: str = "stable"    # 信号趋势 (increasing/decreasing/stable)

    # 质量评分
    overall_health_score: float = 0.0  # 综合健康度 (0-100)
    health_level: str = "NORMAL"       # 健康等级 (EXCELLENT/GOOD/NORMAL/POOR/CRITICAL)

    # 权重建议
    recommended_weight: float = 1.0
    weight_adjustment_reason: str = ""

    # 时间戳
    last_trade_time: Optional[str] = None
    last_updated: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> Dict:
        return {
            "strategy_name": self.strategy_name,
            "recent_trades": self.recent_trades,
            "recent_win_rate": round(self.recent_win_rate, 4),
            "avg_profit_loss_ratio": round(self.avg_profit_loss_ratio, 4),
            "max_consecutive_losses": self.max_consecutive_losses,
            "current_consecutive_losses": self.current_consecutive_losses,
            "regime_fit_score": round(self.regime_fit_score, 4),
            "current_regime": self.current_regime,
            "signal_frequency": round(self.signal_frequency, 2),
            "signal_trend": self.signal_trend,
            "overall_health_score": round(self.overall_health_score, 2),
            "health_level": self.health_level,
            "recommended_weight": round(self.recommended_weight, 3),
            "weight_adjustment_reason": self.weight_adjustment_reason,
            "last_trade_time": self.last_trade_time,
            "last_updated": self.last_updated,
        }


@dataclass
class TradeRecord:
    """交易记录 (用于健康度计算)"""
    trade_id: str
    strategy: str
    code: str
    action: str
    entry_price: float
    exit_price: float
    pnl: float
    pnl_percent: float
    entry_time: str
    exit_time: str
    market_regime: str = ""

    def to_dict(self) -> Dict:
        return {
            "trade_id": self.trade_id,
            "strategy": self.strategy,
            "code": self.code,
            "action": self.action,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "pnl": self.pnl,
            "pnl_percent": self.pnl_percent,
            "entry_time": self.entry_time,
            "exit_time": self.exit_time,
            "market_regime": self.market_regime,
        }


class StrategyHealthTracker:
    """
    策略健康度追踪器

    为每个策略维护滚动窗口统计，用于动态权重调整
    """

    def __init__(
        self,
        window_size: int = 20,       # 滚动窗口大小
        min_trades_for_weight: int = 5,  # 调整权重所需最少交易数
        lookback_hours: int = 72     # 信号频率统计回溯时间
    ):
        """
        初始化健康度追踪器

        Args:
            window_size: 滚动窗口大小 (用于计算胜率)
            min_trades_for_weight: 调整权重所需最少交易数
            lookback_hours: 信号频率统计回溯时间
        """
        self.window_size = window_size
        self.min_trades_for_weight = min_trades_for_weight
        self.lookback_hours = lookback_hours

        # 策略交易历史 {strategy_name: deque[TradeRecord]}
        self._trade_history: Dict[str, deque] = {}

        # 策略信号时间 {strategy_name: deque[timestamp]}
        self._signal_times: Dict[str, deque] = {}

        # 策略健康度缓存 {strategy_name: StrategyHealthMetrics}
        self._health_cache: Dict[str, StrategyHealthMetrics] = {}

        # 当前市场状态
        self._current_regime = "neutral"

        logger.info(
            f"策略健康度追踪器已初始化：窗口={window_size}, "
            f"最少交易={min_trades_for_weight}, 回溯={lookback_hours}h"
        )

    def record_trade(self, trade: TradeRecord):
        """
        记录交易结果

        Args:
            trade: 交易记录
        """
        strategy = trade.strategy

        if strategy not in self._trade_history:
            self._trade_history[strategy] = deque(maxlen=self.window_size * 2)
            self._signal_times[strategy] = deque(maxlen=1000)

        self._trade_history[strategy].append(trade)

        # 记录信号时间 (用于频率统计)
        self._signal_times[strategy].append(trade.entry_time)

        # 清除缓存
        if strategy in self._health_cache:
            del self._health_cache[strategy]

        logger.debug(
            f"[{strategy}] 交易记录：{trade.code} "
            f"盈亏={trade.pnl_percent:.2%}"
        )

    def record_signal(self, strategy: str, timestamp: str = None):
        """
        记录信号产生 (用于频率统计)

        Args:
            strategy: 策略名称
            timestamp: 信号时间
        """
        if timestamp is None:
            timestamp = datetime.now().isoformat()

        if strategy not in self._signal_times:
            self._signal_times[strategy] = deque(maxlen=1000)
            self._trade_history[strategy] = deque(maxlen=self.window_size * 2)

        self._signal_times[strategy].append(timestamp)

    def get_health_metrics(
        self,
        strategy: str,
        current_regime: str = None
    ) -> StrategyHealthMetrics:
        """
        获取策略健康度指标

        Args:
            strategy: 策略名称
            current_regime: 当前市场状态

        Returns:
            策略健康度指标
        """
        # 检查缓存
        if strategy in self._health_cache:
            metrics = self._health_cache[strategy]
            # 如果市场状态变化，重新计算
            if current_regime and current_regime != metrics.current_regime:
                pass  # 重新计算
            else:
                return metrics

        # 获取交易历史
        history = self._trade_history.get(strategy, deque())

        if not history:
            # 无交易历史，返回默认值
            metrics = StrategyHealthMetrics(strategy_name=strategy)
            self._health_cache[strategy] = metrics
            return metrics

        # 转换为列表便于计算
        trades = list(history)

        # 计算核心指标
        recent_trades = len(trades)

        # 胜率 (最近 window_size 笔)
        recent = trades[-self.window_size:] if len(trades) > self.window_size else trades
        wins = sum(1 for t in recent if t.pnl_percent > 0)
        recent_win_rate = wins / len(recent) if recent else 0

        # 平均盈亏比
        total_profit = sum(t.pnl_percent for t in trades if t.pnl_percent > 0)
        total_loss = abs(sum(t.pnl_percent for t in trades if t.pnl_percent < 0))
        avg_profit_loss_ratio = (total_profit / wins) / (total_loss / (len(trades) - wins)) if wins > 0 and total_loss > 0 else 1.0

        # 最大连续亏损
        max_consecutive = 0
        current_consecutive = 0
        for t in reversed(trades):
            if t.pnl_percent < 0:
                current_consecutive += 1
                max_consecutive = max(max_consecutive, current_consecutive)
            else:
                current_consecutive = 0

        # 信号频率
        signal_freq = self._calculate_signal_frequency(strategy)
        signal_trend = self._calculate_signal_trend(strategy)

        # 市场适配度
        regime = current_regime or self._current_regime
        regime_fit = self._calculate_regime_fit(strategy, regime)

        # 综合健康度评分
        health_score = self._calculate_health_score(
            win_rate=recent_win_rate,
            profit_loss_ratio=avg_profit_loss_ratio,
            max_consecutive_losses=max_consecutive,
            regime_fit=regime_fit,
            signal_frequency=signal_freq,
        )

        # 健康等级
        health_level = self._get_health_level(health_score)

        # 权重建议
        rec_weight, reason = self._calculate_recommended_weight(
            health_score=health_score,
            win_rate=recent_win_rate,
            consecutive_losses=max_consecutive,
            trade_count=len(trades),
        )

        # 最后交易时间
        last_trade_time = trades[-1].entry_time if trades else None

        metrics = StrategyHealthMetrics(
            strategy_name=strategy,
            recent_trades=len(recent),
            recent_win_rate=recent_win_rate,
            avg_profit_loss_ratio=avg_profit_loss_ratio,
            max_consecutive_losses=max_consecutive,
            current_consecutive_losses=current_consecutive,
            regime_fit_score=regime_fit,
            current_regime=regime,
            signal_frequency=signal_freq,
            signal_trend=signal_trend,
            overall_health_score=health_score,
            health_level=health_level,
            recommended_weight=rec_weight,
            weight_adjustment_reason=reason,
            last_trade_time=last_trade_time,
        )

        self._health_cache[strategy] = metrics
        return metrics

    def _calculate_signal_frequency(self, strategy: str) -> float:
        """计算信号频率 (signals/hour)"""
        times = self._signal_times.get(strategy, deque())
        if not times:
            return 0.0

        now = datetime.now()
        cutoff = now - timedelta(hours=self.lookback_hours)

        # 统计回溯期内的信号数
        recent_signals = 0
        for ts in times:
            try:
                signal_time = datetime.fromisoformat(ts)
                if signal_time > cutoff:
                    recent_signals += 1
            except Exception:
                continue

        return recent_signals / self.lookback_hours

    def _calculate_signal_trend(self, strategy: str) -> str:
        """计算信号趋势"""
        times = self._signal_times.get(strategy, deque())
        if not times or len(times) < 6:
            return "stable"

        # 比较前一半和后一半的频率
        time_list = list(times)
        mid = len(time_list) // 2

        first_half = len(time_list[:mid])
        second_half = len(time_list[mid:])

        if second_half > first_half * 1.2:
            return "increasing"
        elif second_half < first_half * 0.8:
            return "decreasing"
        else:
            return "stable"

    def _calculate_regime_fit(self, strategy: str, regime: str) -> float:
        """
        计算策略在当前市场状态下的适配度

        返回 0-1 之间的分数
        """
        history = self._trade_history.get(strategy, deque())
        if not history:
            return 0.5  # 默认中等适配

        # 统计该市场状态下的胜率
        regime_trades = [t for t in history if t.market_regime == regime]
        if not regime_trades:
            return 0.5  # 无数据时返回默认值

        wins = sum(1 for t in regime_trades if t.pnl_percent > 0)
        return wins / len(regime_trades)

    def _calculate_health_score(
        self,
        win_rate: float,
        profit_loss_ratio: float,
        max_consecutive_losses: int,
        regime_fit: float,
        signal_frequency: float,
    ) -> float:
        """
        计算综合健康度评分 (0-100)

        权重分配:
        - 胜率: 30%
        - 盈亏比: 25%
        - 连续亏损: 20%
        - 市场适配: 15%
        - 信号稳定性: 10%
        """
        # 胜率得分 (0-30)
        win_score = min(win_rate * 60, 30)  # 50% 胜率 → 30 分

        # 盈亏比得分 (0-25)
        pl_score = min(profit_loss_ratio * 12.5, 25)  # 2.0 → 25 分

        # 连续亏损得分 (0-20) - 越少越好
        consecutive_score = max(0, 20 - max_consecutive_losses * 3)

        # 市场适配得分 (0-15)
        regime_score = regime_fit * 15

        # 信号稳定性得分 (0-10) - 适中最好
        if 0.5 <= signal_frequency <= 3.0:
            signal_score = 10
        elif signal_frequency < 0.5:
            signal_score = 5
        else:
            signal_score = max(0, 10 - (signal_frequency - 3.0) * 2)

        total = win_score + pl_score + consecutive_score + regime_score + signal_score
        return round(total, 2)

    def _get_health_level(self, health_score: float) -> str:
        """根据健康度评分返回健康等级"""
        if health_score >= 80:
            return "EXCELLENT"
        elif health_score >= 60:
            return "GOOD"
        elif health_score >= 40:
            return "NORMAL"
        elif health_score >= 20:
            return "POOR"
        else:
            return "CRITICAL"

    def _calculate_recommended_weight(
        self,
        health_score: float,
        win_rate: float,
        consecutive_losses: int,
        trade_count: int,
    ) -> tuple:
        """
        计算建议权重

        Returns:
            (建议权重值，调整原因)
        """
        # 基础权重 1.0
        base_weight = 1.0

        reason_parts = []

        # 交易笔数不足时不做大幅调整
        if trade_count < self.min_trades_for_weight:
            return base_weight, f"交易笔数不足 ({trade_count}<{self.min_trades_for_weight})"

        # 健康度调整
        if health_score >= 80:
            weight_adjust = 0.5
            reason_parts.append("表现优秀")
        elif health_score >= 60:
            weight_adjust = 0.2
            reason_parts.append("表现良好")
        elif health_score < 40:
            weight_adjust = -0.3
            reason_parts.append("表现欠佳")
        else:
            weight_adjust = 0
            reason_parts.append("表现正常")

        # 连续亏损调整
        if consecutive_losses >= 5:
            weight_adjust -= 0.3
            reason_parts.append(f"连续亏损{consecutive_losses}笔")

        # 胜率调整
        if win_rate >= 0.6:
            weight_adjust += 0.2
        elif win_rate < 0.4:
            weight_adjust -= 0.2

        # 限制权重范围
        recommended = max(0.3, min(2.0, base_weight + weight_adjust))

        return round(recommended, 3), "; ".join(reason_parts)

    def update_current_regime(self, regime: str):
        """更新当前市场状态"""
        self._current_regime = regime
        logger.info(f"更新市场状态：{regime}")

    def get_all_health_metrics(self) -> Dict[str, StrategyHealthMetrics]:
        """获取所有策略的健康度指标"""
        all_metrics = {}
        for strategy in self._trade_history.keys():
            all_metrics[strategy] = self.get_health_metrics(strategy)
        return all_metrics

    def get_weight_recommendations(self) -> Dict[str, float]:
        """获取策略权重建议"""
        recommendations = {}
        for strategy, metrics in self.get_all_health_metrics().items():
            recommendations[strategy] = metrics.recommended_weight
        return recommendations

    def get_health_report(self) -> Dict:
        """生成健康度报告"""
        all_metrics = self.get_all_health_metrics()

        report = {
            "generated_at": datetime.now().isoformat(),
            "current_regime": self._current_regime,
            "strategies": {},
            "summary": {},
        }

        # 各策略详情
        for strategy, metrics in all_metrics.items():
            report["strategies"][strategy] = metrics.to_dict()

        # 汇总统计
        if all_metrics:
            excellent = sum(1 for m in all_metrics.values() if m.health_level == "EXCELLENT")
            good = sum(1 for m in all_metrics.values() if m.health_level == "GOOD")
            poor = sum(1 for m in all_metrics.values() if m.health_level in ["POOR", "CRITICAL"])

            report["summary"] = {
                "total_strategies": len(all_metrics),
                "excellent_count": excellent,
                "good_count": good,
                "poor_count": poor,
                "recommended_weights": self.get_weight_recommendations(),
            }

        return report


# ==================== 全局单例 ====================

_health_tracker: Optional[StrategyHealthTracker] = None


def get_strategy_health_tracker(
    window_size: int = 20,
    min_trades: int = 5
) -> StrategyHealthTracker:
    """获取健康度追踪器单例"""
    global _health_tracker
    if _health_tracker is None:
        _health_tracker = StrategyHealthTracker(window_size, min_trades)
    return _health_tracker


def init_strategy_health_tracker(
    window_size: int = 20,
    min_trades: int = 5
) -> StrategyHealthTracker:
    """初始化健康度追踪器"""
    global _health_tracker
    _health_tracker = StrategyHealthTracker(window_size, min_trades)
    return _health_tracker
