"""
信号质量评分器 (Signal Scorer)

核心功能:
1. 计算信号质量分数 (0-1)
2. 多维度评分：历史准确率、策略置信度、多因子对齐度等
3. 质量等级判定：HIGH / MEDIUM / LOW
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from datetime import datetime
from enum import Enum

logger = logging.getLogger("ftseek.quality.scorer")
# 2026-04-10: propagate=True (默认), 通过 ftseek 父级 logger 输出


class QualityLevel(Enum):
    """质量等级"""
    HIGH = "high"       # ≥0.75, 优质信号，优先推送
    MEDIUM = "medium"   # 0.50-0.74, 普通信号，正常推送
    LOW = "low"         # <0.50, 低质信号，过滤或降权


@dataclass
class SignalQuality:
    """信号质量评分结果"""
    code: str                              # 股票代码
    signal_id: str                         # 信号 ID
    score: float = 0.0                     # 综合质量分 (0-1)
    level: QualityLevel = QualityLevel.LOW  # 质量等级

    # 各维度得分
    historical_accuracy: float = 0.0       # 历史准确率 (0-1)
    strategy_confidence: float = 0.0       # 策略置信度 (0-1)
    multi_factor_alignment: float = 0.0    # 多因子对齐度 (0-1)
    market_regime_fit: float = 0.0         # 市场状态适配度 (0-1)
    news_sentiment_confirm: float = 0.0    # 新闻情绪确认 (0-1)

    # 元数据
    strategy_name: str = ""                # 策略名称
    market_regime: str = ""                # 市场状态
    timestamp: str = ""                    # 评分时间

    # 建议
    recommendation: str = ""               # 推送建议

    def __post_init__(self):
        """初始化后处理"""
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat()

        # 根据分数判定等级
        if self.score >= 0.75:
            self.level = QualityLevel.HIGH
            self.recommendation = "优质信号，优先推送"
        elif self.score >= 0.50:
            self.level = QualityLevel.MEDIUM
            self.recommendation = "普通信号，正常推送"
        else:
            self.level = QualityLevel.LOW
            self.recommendation = "低质信号，建议过滤"


@dataclass
class ScorerWeights:
    """评分权重配置"""
    historical_accuracy: float = 0.30      # 历史准确率权重 30%
    strategy_confidence: float = 0.20      # 策略置信度权重 20%
    multi_factor_alignment: float = 0.25   # 多因子对齐度权重 25%
    market_regime_fit: float = 0.15        # 市场状态适配度权重 15%
    news_sentiment_confirm: float = 0.10   # 新闻情绪确认权重 10%

    def validate(self) -> bool:
        """验证权重和是否为 1"""
        total = (
            self.historical_accuracy +
            self.strategy_confidence +
            self.multi_factor_alignment +
            self.market_regime_fit +
            self.news_sentiment_confirm
        )
        return abs(total - 1.0) < 0.01


class SignalScorer:
    """
    信号质量评分器

    评分公式:
    quality_score = Σ(weight_i × score_i)
    """

    def __init__(self, weights: Optional[ScorerWeights] = None):
        """
        初始化评分器

        Args:
            weights: 权重配置，默认使用 ScorerWeights 默认值
        """
        self.weights = weights or ScorerWeights()
        if not self.weights.validate():
            logger.warning("权重和不为 1，已自动归一化")
            self._normalize_weights()

        # 延迟导入，避免循环依赖
        self._historical_tracker = None
        self._regime_adapter = None

        logger.info("信号质量评分器已初始化")
        logger.info(f"  权重配置：历史={self.weights.historical_accuracy:.0%}, "
                   f"置信度={self.weights.strategy_confidence:.0%}, "
                   f"对齐度={self.weights.multi_factor_alignment:.0%}, "
                   f"适配度={self.weights.market_regime_fit:.0%}, "
                   f"情绪={self.weights.news_sentiment_confirm:.0%}")

    def _normalize_weights(self):
        """归一化权重"""
        total = (
            self.weights.historical_accuracy +
            self.weights.strategy_confidence +
            self.weights.multi_factor_alignment +
            self.weights.market_regime_fit +
            self.weights.news_sentiment_confirm
        )
        if total > 0:
            self.weights.historical_accuracy /= total
            self.weights.strategy_confidence /= total
            self.weights.multi_factor_alignment /= total
            self.weights.market_regime_fit /= total
            self.weights.news_sentiment_confirm /= total

    def _get_historical_tracker(self):
        """获取历史追踪器 (延迟加载)"""
        if self._historical_tracker is None:
            from .historical_tracker import HistoricalTracker
            self._historical_tracker = HistoricalTracker()
        return self._historical_tracker

    def _get_regime_adapter(self):
        """获取市场状态适配器 (延迟加载)"""
        if self._regime_adapter is None:
            from .regime_adapter import RegimeAdapter
            self._regime_adapter = RegimeAdapter()
        return self._regime_adapter

    def evaluate(self, signal: Any, market_regime: str = "default",
                 extra_data: Optional[Dict] = None) -> SignalQuality:
        """
        评估信号质量

        Args:
            signal: 交易信号对象 (包含 code, strategy, confidence 等属性)
            market_regime: 市场状态 (strong_trend_up/range_bound/strong_trend_down 等)
            extra_data: 额外数据 (包含多因子对齐度、新闻情绪等)

        Returns:
            SignalQuality: 信号质量评分结果
        """
        # 提取信号信息
        code = getattr(signal, 'code', '')
        strategy = getattr(signal, 'strategy', 'unknown')
        confidence = getattr(signal, 'confidence', 0.0)
        signal_id = getattr(signal, 'id', '') or getattr(signal, 'signal_id', '') or f"{code}_{datetime.now().strftime('%Y%m%d%H%M%S')}"

        # 1. 历史准确率得分
        tracker = self._get_historical_tracker()
        historical_accuracy = tracker.get_strategy_accuracy(strategy)

        # 2. 策略置信度得分 (归一化到 0-1)
        strategy_confidence = min(1.0, max(0.0, confidence))

        # 3. 多因子对齐度得分 (从 extra_data 获取或计算)
        multi_factor_alignment = self._calculate_factor_alignment(signal, extra_data)

        # 4. 市场状态适配度得分
        regime_adapter = self._get_regime_adapter()
        market_regime_fit = regime_adapter.get_fit_score(strategy, market_regime)

        # 5. 新闻情绪确认得分 (从 extra_data 获取)
        news_sentiment_confirm = self._get_sentiment_confirm(extra_data)

        # 计算综合得分
        quality_score = (
            self.weights.historical_accuracy * historical_accuracy +
            self.weights.strategy_confidence * strategy_confidence +
            self.weights.multi_factor_alignment * multi_factor_alignment +
            self.weights.market_regime_fit * market_regime_fit +
            self.weights.news_sentiment_confirm * news_sentiment_confirm
        )

        # 构建评分结果
        result = SignalQuality(
            code=code,
            signal_id=signal_id,
            score=round(quality_score, 3),
            historical_accuracy=round(historical_accuracy, 3),
            strategy_confidence=round(strategy_confidence, 3),
            multi_factor_alignment=round(multi_factor_alignment, 3),
            market_regime_fit=round(market_regime_fit, 3),
            news_sentiment_confirm=round(news_sentiment_confirm, 3),
            strategy_name=strategy,
            market_regime=market_regime,
        )

        logger.debug(f"[信号质量] {code} | 策略={strategy} | 质量分={quality_score:.3f} ({result.level.value})")

        return result

    def _calculate_factor_alignment(self, signal: Any, extra_data: Optional[Dict] = None) -> float:
        """
        计算多因子对齐度

        Args:
            signal: 交易信号
            extra_data: 额外数据 (包含各因子得分)

        Returns:
            对齐度得分 (0-1)
        """
        if extra_data is None:
            extra_data = {}

        # 从 extra_data 获取各因子得分
        factors = extra_data.get('factors', {})

        if not factors:
            # 如果没有因子数据，使用信号的 extra 字段
            signal_extra = getattr(signal, 'extra', {})
            if isinstance(signal_extra, dict):
                factors = signal_extra.get('factors', {})

        if not factors:
            # 默认返回中等对齐度
            return 0.5

        # 计算因子一致性
        # factors 格式：{'rsi': 0.7, 'macd': 0.6, 'bb': 0.8, ...}
        # 正值表示看多，负值表示看空

        scores = list(factors.values())
        if not scores:
            return 0.5

        # 计算标准差，标准差越小表示因子越一致
        avg = sum(scores) / len(scores)
        variance = sum((s - avg) ** 2 for s in scores) / len(scores)
        std_dev = variance ** 0.5

        # 标准差为 0 表示完全一致，转换为对齐度得分
        # std_dev=0 → alignment=1.0, std_dev=0.5 → alignment=0.0
        alignment = max(0.0, 1.0 - std_dev * 2)

        return alignment

    def _get_sentiment_confirm(self, extra_data: Optional[Dict] = None) -> float:
        """
        获取新闻情绪确认得分

        Args:
            extra_data: 额外数据 (包含 sentiment_score)

        Returns:
            情绪确认得分 (0-1)
        """
        if extra_data is None:
            extra_data = {}

        sentiment_score = extra_data.get('sentiment_score', 0.0)

        # 将情绪得分 (-1 到 1) 转换为确认得分 (0 到 1)
        # sentiment=0.5 → confirm=0.75, sentiment=-0.5 → confirm=0.25
        confirm_score = 0.5 + sentiment_score * 0.5

        return max(0.0, min(1.0, confirm_score))

    def record_outcome(self, code: str, strategy: str, signal_quality: SignalQuality,
                      exit_price: float, entry_price: float, action: str):
        """
        记录信号结果 (用于后续历史准确率计算)

        Args:
            code: 股票代码
            strategy: 策略名称
            signal_quality: 信号质量对象
            exit_price: 平仓价格
            entry_price: 开仓价格
            action: 买卖方向 (BUY/SELL)
        """
        # 计算盈亏
        if action == 'BUY':
            pnl_pct = (exit_price - entry_price) / entry_price
        else:
            pnl_pct = (entry_price - exit_price) / entry_price

        # 记录到历史追踪器
        tracker = self._get_historical_tracker()
        tracker.record_outcome(
            code=code,
            strategy=strategy,
            signal_id=signal_quality.signal_id,
            pnl_pct=pnl_pct,
            quality_score=signal_quality.score,
        )

        logger.debug(f"[结果记录] {code} | 策略={strategy} | 盈亏={pnl_pct:.2%} | 质量分={signal_quality.score:.3f}")
