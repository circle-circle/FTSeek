"""
ConfidenceScorer - 置信度评分系统

对交易信号进行多维度置信度量化评分，整合技术面、催化事件、情绪面和量能确认
四个维度的分数，输出综合置信度级别和仓位建议。

评分维度:
- 技术面 (40%): 形态完成度、突破有效性、均线排列、RSI/MACD 指标
- 催化事件 (30%): 财报超预期、FDA 审批、产品发布、分析师评级
- 情绪面 (20%): 新闻情绪、社交媒体热度、机构资金流向
- 量能确认 (10%): 盘前量比、成交量放大、资金净流入
"""

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class ConfidenceFactors:
    """
    置信度因子数据类

    Attributes:
        technical: 技术面分数 (0-1)
        catalyst: 催化事件分数 (0-1)
        sentiment: 情绪面分数 (0-1)
        volume: 量能确认分数 (0-1)
    """
    technical: float
    catalyst: float
    sentiment: float
    volume: float

    def __post_init__(self) -> None:
        """验证因子值是否在有效范围内"""
        self._validate_factor('technical', self.technical)
        self._validate_factor('catalyst', self.catalyst)
        self._validate_factor('sentiment', self.sentiment)
        self._validate_factor('volume', self.volume)

    @staticmethod
    def _validate_factor(name: str, value: float) -> None:
        """
        验证单个因子值

        Args:
            name: 因子名称
            value: 因子值

        Raises:
            ValueError: 当因子值不在 0-1 范围内时
        """
        if not 0.0 <= value <= 1.0:
            raise ValueError(
                f"因子 '{name}' 的值 {value} 超出有效范围 [0.0, 1.0]"
            )


@dataclass
class ConfidenceResult:
    """
    置信度评分结果数据类

    Attributes:
        score: 综合评分 (0-1)
        level: 置信度级别 (STRONG/RECOMMENDED/WATCH/SKIP)
        breakdown: 各维度分数明细
        recommendation: 建议操作
    """
    score: float
    level: str
    breakdown: Dict[str, float]
    recommendation: str


class ConfidenceScorer:
    """
    置信度评分器

    根据多维度因子计算综合置信度分数，并提供级别分类和仓位建议。

    Attributes:
        weights: 各维度权重配置

    Example:
        >>> scorer = ConfidenceScorer()
        >>> factors = ConfidenceFactors(
        ...     technical=0.9,
        ...     catalyst=0.8,
        ...     sentiment=0.7,
        ...     volume=0.6
        ... )
        >>> result = scorer.generate_result(factors)
        >>> print(f"置信度：{result.level}, 评分：{result.score:.2f}")
    """

    # 默认权重配置
    DEFAULT_WEIGHTS = {
        'technical': 0.4,
        'catalyst': 0.3,
        'sentiment': 0.2,
        'volume': 0.1
    }

    # 置信度级别阈值
    LEVEL_THRESHOLDS = {
        'STRONG': 0.80,
        'RECOMMENDED': 0.60,
        'WATCH': 0.40,
        'SKIP': 0.0
    }

    # 仓位比例配置
    POSITION_RATIOS = {
        'STRONG': 1.0,
        'RECOMMENDED': 0.7,
        'WATCH': 0.3,
        'SKIP': 0.0
    }

    # 各级别建议操作
    RECOMMENDATIONS = {
        'STRONG': '强烈建议建仓 - 高置信度信号，可满仓操作',
        'RECOMMENDED': '建议建仓 - 中高置信度信号，建议 7 成仓位',
        'WATCH': '观望为主 - 中等置信度信号，建议轻仓试探',
        'SKIP': '跳过 - 低置信度信号，不建议操作'
    }

    def __init__(self, weights: Optional[Dict[str, float]] = None) -> None:
        """
        初始化评分器

        Args:
            weights: 各维度权重配置
                   默认：technical=0.4, catalyst=0.3, sentiment=0.2, volume=0.1

        Example:
            >>> # 使用默认权重
            >>> scorer = ConfidenceScorer()
            >>>
            >>> # 使用自定义权重
            >>> custom_weights = {
            ...     'technical': 0.5,
            ...     'catalyst': 0.3,
            ...     'sentiment': 0.1,
            ...     'volume': 0.1
            ... }
            >>> scorer = ConfidenceScorer(weights=custom_weights)
        """
        self.weights = weights if weights is not None else self.DEFAULT_WEIGHTS.copy()

    def calculate(self, factors: ConfidenceFactors) -> float:
        """
        计算综合置信度

        使用加权平均公式计算综合分数：
        score = (
            technical × 0.4 +
            catalyst × 0.3 +
            sentiment × 0.2 +
            volume × 0.1
        )

        Args:
            factors: 置信度因子

        Returns:
            float: 综合置信度分数 (0-1)

        Example:
            >>> scorer = ConfidenceScorer()
            >>> factors = ConfidenceFactors(0.8, 0.6, 0.7, 0.5)
            >>> score = scorer.calculate(factors)
            >>> print(f"{score:.2f}")
            0.65
        """
        score = (
            factors.technical * self.weights['technical'] +
            factors.catalyst * self.weights['catalyst'] +
            factors.sentiment * self.weights['sentiment'] +
            factors.volume * self.weights['volume']
        )

        # 确保分数在 0-1 范围内
        return max(0.0, min(1.0, score))

    def get_level(self, score: float) -> str:
        """
        获取置信度级别

        级别划分:
        - STRONG:       score >= 0.80
        - RECOMMENDED:  0.60 <= score < 0.80
        - WATCH:        0.40 <= score < 0.60
        - SKIP:         score < 0.40

        Args:
            score: 综合置信度分数 (0-1)

        Returns:
            str: 置信度级别

        Example:
            >>> scorer = ConfidenceScorer()
            >>> scorer.get_level(0.85)
            'STRONG'
            >>> scorer.get_level(0.65)
            'RECOMMENDED'
            >>> scorer.get_level(0.50)
            'WATCH'
            >>> scorer.get_level(0.30)
            'SKIP'
        """
        if score >= self.LEVEL_THRESHOLDS['STRONG']:
            return 'STRONG'
        elif score >= self.LEVEL_THRESHOLDS['RECOMMENDED']:
            return 'RECOMMENDED'
        elif score >= self.LEVEL_THRESHOLDS['WATCH']:
            return 'WATCH'
        else:
            return 'SKIP'

    def get_position_ratio(self, level: str) -> float:
        """
        根据置信度级别获取建议仓位比例

        仓位配置:
        - STRONG:       100%
        - RECOMMENDED:  70%
        - WATCH:        30%
        - SKIP:         0%

        Args:
            level: 置信度级别

        Returns:
            float: 建议仓位比例 (0-1)

        Raises:
            ValueError: 当级别无效时

        Example:
            >>> scorer = ConfidenceScorer()
            >>> scorer.get_position_ratio('STRONG')
            1.0
            >>> scorer.get_position_ratio('RECOMMENDED')
            0.7
        """
        if level not in self.POSITION_RATIOS:
            raise ValueError(
                f"无效的置信度级别：{level}。"
                f"有效级别：{list(self.POSITION_RATIOS.keys())}"
            )
        return self.POSITION_RATIOS[level]

    def generate_result(self, factors: ConfidenceFactors) -> ConfidenceResult:
        """
        生成完整评分结果

        综合计算置信度分数、级别、维度明细和操作建议。

        Args:
            factors: 置信度因子

        Returns:
            ConfidenceResult: 完整的评分结果

        Example:
            >>> scorer = ConfidenceScorer()
            >>> factors = ConfidenceFactors(0.9, 0.8, 0.7, 0.6)
            >>> result = scorer.generate_result(factors)
            >>> print(f"级别：{result.level}")
            >>> print(f"评分：{result.score:.2f}")
            >>> print(f"建议：{result.recommendation}")
        """
        # 计算综合分数
        score = self.calculate(factors)

        # 获取级别
        level = self.get_level(score)

        # 构建维度明细
        breakdown = {
            'technical': factors.technical,
            'catalyst': factors.catalyst,
            'sentiment': factors.sentiment,
            'volume': factors.volume
        }

        # 获取建议操作
        recommendation = self.RECOMMENDATIONS[level]

        return ConfidenceResult(
            score=score,
            level=level,
            breakdown=breakdown,
            recommendation=recommendation
        )
