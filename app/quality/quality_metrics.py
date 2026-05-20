"""
质量指标定义 (Quality Metrics)

定义信号质量的各种指标和计算方法
"""

import logging
from typing import Dict, List, Optional
from dataclasses import dataclass
from datetime import datetime, timedelta

logger = logging.getLogger("ftseek.quality.metrics")
# 2026-04-10: propagate=True (默认), 通过 ftseek 父级 logger 输出


@dataclass
class QualityThreshold:
    """质量阈值配置"""
    # 高质量信号阈值
    high_min: float = 0.75       # ≥0.75 为高质量

    # 中等质量信号阈值
    medium_min: float = 0.50     # 0.50-0.74 为中等质量

    # 低质量信号阈值
    low_max: float = 0.50        # <0.50 为低质量

    # 维度阈值
    min_historical_accuracy: float = 0.55   # 2026-05-19: 提高门槛 (原0.45, 低于50%胜率不应通过)
    min_confidence: float = 0.50            # 最低置信度
    min_factor_alignment: float = 0.50      # 2026-05-19: 提高门槛 (原0.40, 要求更严格因子一致性)


@dataclass
class DimensionScore:
    """维度得分详情"""
    name: str                    # 维度名称
    score: float                 # 得分 (0-1)
    weight: float                # 权重
    weighted_score: float        # 加权得分
    status: str = "normal"       # 状态：high/normal/low
    details: Dict = None         # 详细数据

    def __post_init__(self):
        if self.details is None:
            self.details = {}
        self.weighted_score = self.score * self.weight

        if self.score >= 0.75:
            self.status = "high"
        elif self.score >= 0.50:
            self.status = "normal"
        else:
            self.status = "low"


class QualityMetrics:
    """
    质量指标计算器

    提供各维度得分的详细计算和解释
    """

    def __init__(self, thresholds: Optional[QualityThreshold] = None):
        """
        初始化指标计算器

        Args:
            thresholds: 阈值配置
        """
        self.thresholds = thresholds or QualityThreshold()

        # 维度说明
        self.dimension_descriptions = {
            "historical_accuracy": "历史准确率 - 基于该策略过往信号的实际表现",
            "strategy_confidence": "策略置信度 - 策略自身对信号的确认程度",
            "multi_factor_alignment": "多因子对齐度 - 各技术因子的一致性",
            "market_regime_fit": "市场状态适配度 - 策略与当前市场行情的匹配度",
            "news_sentiment_confirm": "新闻情绪确认 - 新闻情绪对信号方向的支持度",
        }

        logger.info("质量指标计算器已初始化")

    def calculate_all_dimensions(
        self,
        historical_accuracy: float,
        strategy_confidence: float,
        multi_factor_alignment: float,
        market_regime_fit: float,
        news_sentiment_confirm: float,
        weights: Dict[str, float]
    ) -> List[DimensionScore]:
        """
        计算所有维度得分

        Args:
            historical_accuracy: 历史准确率
            strategy_confidence: 策略置信度
            multi_factor_alignment: 多因子对齐度
            market_regime_fit: 市场状态适配度
            news_sentiment_confirm: 新闻情绪确认
            weights: 各维度权重

        Returns:
            维度得分列表
        """
        dimensions = {
            "historical_accuracy": historical_accuracy,
            "strategy_confidence": strategy_confidence,
            "multi_factor_alignment": multi_factor_alignment,
            "market_regime_fit": market_regime_fit,
            "news_sentiment_confirm": news_sentiment_confirm,
        }

        scores = []
        for name, score in dimensions.items():
            weight = weights.get(name, 0.0)
            dim_score = DimensionScore(
                name=name,
                score=score,
                weight=weight,
                weighted_score=score * weight,
                details={"raw_score": score}
            )
            scores.append(dim_score)

        return scores

    def get_dimension_analysis(self, score: DimensionScore) -> str:
        """
        获取维度得分分析

        Args:
            score: 维度得分

        Returns:
            分析文本
        """
        description = self.dimension_descriptions.get(score.name, "")

        if score.status == "high":
            analysis = f"✅ {score.name}: {score.score:.1%} (优秀)"
        elif score.status == "normal":
            analysis = f"✓ {score.name}: {score.score:.1%} (正常)"
        else:
            analysis = f"⚠ {score.name}: {score.score:.1%} (偏低)"

        return f"{analysis} - {description}"

    def get_quality_report(self, overall_score: float, dimension_scores: List[DimensionScore]) -> str:
        """
        生成质量报告

        Args:
            overall_score: 综合得分
            dimension_scores: 维度得分列表

        Returns:
            质量报告文本
        """
        # 判定质量等级
        if overall_score >= self.thresholds.high_min:
            level = "高质量"
            emoji = "✅"
        elif overall_score >= self.thresholds.medium_min:
            level = "中等质量"
            emoji = "✓"
        else:
            level = "低质量"
            emoji = "⚠"

        # 构建报告
        lines = [
            f"{emoji} 信号质量报告：{level} (综合得分：{overall_score:.1%})",
            "",
            "维度分析:"
        ]

        for dim in dimension_scores:
            analysis = self.get_dimension_analysis(dim)
            lines.append(f"  {analysis} (权重：{dim.weight:.0%})")

        # 计算加权得分总和
        total_weighted = sum(d.weighted_score for d in dimension_scores)
        lines.append(f"")
        lines.append(f"加权得分总和：{total_weighted:.3f}")

        # 识别弱项
        weak_dimensions = [d for d in dimension_scores if d.status == "low"]
        if weak_dimensions:
            lines.append("")
            lines.append("需改进的弱项:")
            for dim in weak_dimensions:
                lines.append(f"  - {dim.name}: {dim.score:.1%}")

        return "\n".join(lines)

    def check_thresholds(self, scores: Dict[str, float]) -> Dict[str, bool]:
        """
        检查各维度是否达到阈值

        Args:
            scores: 各维度得分字典

        Returns:
            检查结果字典 {维度名：是否达标}
        """
        checks = {
            "historical_accuracy": scores.get("historical_accuracy", 0) >= self.thresholds.min_historical_accuracy,
            "confidence": scores.get("strategy_confidence", 0) >= self.thresholds.min_confidence,
            "factor_alignment": scores.get("multi_factor_alignment", 0) >= self.thresholds.min_factor_alignment,
        }

        return checks

    def get_rejection_reason(self, overall_score: float, dimension_scores: List[DimensionScore]) -> str:
        """
        获取拒绝原因

        Args:
            overall_score: 综合得分
            dimension_scores: 维度得分列表

        Returns:
            拒绝原因文本
        """
        if overall_score >= self.thresholds.medium_min:
            return ""  # 不需要拒绝

        reasons = []

        # 检查综合得分
        if overall_score < self.thresholds.low_max:
            reasons.append(f"综合得分过低 ({overall_score:.1%} < {self.thresholds.low_max:.0%})")

        # 检查弱项
        for dim in dimension_scores:
            if dim.status == "low":
                reasons.append(f"{dim.name} 得分偏低 ({dim.score:.1%})")

        return "; ".join(reasons) if reasons else "未知原因"
