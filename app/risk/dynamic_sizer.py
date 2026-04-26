#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
动态仓位计算器 (Dynamic Position Sizer)

功能:
- 根据信号质量 (arb_score, news_sentiment) 动态计算仓位
- 动态调整止损/止盈参数
- 支持跨市场套利和事件驱动策略

核心逻辑:
信号质量高 → 仓位大 + 止损紧 + 止盈宽
信号质量低 → 仓位小 + 止损宽 + 止盈紧

使用示例:
    sizer = DynamicPositionSizer()
    params = sizer.calculate(
        arb_score=0.85,
        news_sentiment=0.7,
        base_position_pct=0.05,
        volatility=0.25
    )
    # params = {
    #     'position_pct': 0.08,    # 仓位 8%
    #     'stop_loss_pct': -0.015, # 止损 1.5%
    #     'take_profit_pct': 0.04, # 止盈 4%
    #     'confidence': 0.82
    # }
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, Optional
from enum import Enum

logger = logging.getLogger("ftseek.risk.dynamic_sizer")
logger.propagate = False


class SignalQualityTier(str, Enum):
    """信号质量等级"""
    EXCELLENT = "excellent"  # 极佳 (arb_score > 0.8)
    GOOD = "good"            # 良好 (0.6-0.8)
    FAIR = "fair"            # 一般 (0.4-0.6)
    POOR = "poor"            # 较差 (< 0.4)


@dataclass
class DynamicRiskParams:
    """动态风险参数"""
    # 仓位参数
    position_pct: float = 0.05      # 仓位百分比
    max_position_pct: float = 0.10  # 最大仓位上限

    # 止损/止盈
    stop_loss_pct: float = -0.02    # 止损百分比 (负数)
    take_profit_pct: float = 0.03   # 止盈百分比
    trailing_stop_pct: float = 0.01 # 追踪止损百分比

    # 持仓时间
    max_holding_minutes: int = 30   # 最长持仓时间
    force_exit_time: str = "10:00"  # 强制退出时间

    # 滑点控制
    max_slippage_pct: float = 0.005  # 最大滑点

    # 信号质量
    signal_quality: SignalQualityTier = SignalQualityTier.FAIR
    confidence: float = 0.5  # 置信度

    # 原始输入
    input_arb_score: float = 0.0
    input_sentiment: float = 0.0
    input_volatility: float = 0.0

    def to_dict(self) -> Dict:
        return {
            'position_pct': self.position_pct,
            'max_position_pct': self.max_position_pct,
            'stop_loss_pct': self.stop_loss_pct,
            'take_profit_pct': self.take_profit_pct,
            'trailing_stop_pct': self.trailing_stop_pct,
            'max_holding_minutes': self.max_holding_minutes,
            'force_exit_time': self.force_exit_time,
            'max_slippage_pct': self.max_slippage_pct,
            'signal_quality': self.signal_quality.value,
            'confidence': self.confidence,
            'input_arb_score': self.input_arb_score,
            'input_sentiment': self.input_sentiment,
            'input_volatility': self.input_volatility,
        }


class DynamicPositionSizer:
    """
    动态仓位计算器

    根据信号质量动态调整风险参数
    """

    # 信号质量阈值
    EXCELLENT_THRESHOLD = 0.80  # 极佳
    GOOD_THRESHOLD = 0.60       # 良好
    FAIR_THRESHOLD = 0.40       # 一般

    # 基础参数
    BASE_POSITION_PCT = 0.05     # 基础仓位 5%
    BASE_STOP_LOSS = -0.02       # 基础止损 2%
    BASE_TAKE_PROFIT = 0.03      # 基础止盈 3%

    def __init__(self, config: Dict = None):
        """
        初始化计算器

        Args:
            config: 配置字典 (可选)
        """
        self.config = config or {}

        # 从配置加载参数 (支持覆盖默认值)
        self.base_position_pct = self.config.get('base_position_pct', self.BASE_POSITION_PCT)
        self.max_position_pct = self.config.get('max_position_pct', 0.10)
        self.base_stop_loss = self.config.get('base_stop_loss', self.BASE_STOP_LOSS)
        self.base_take_profit = self.config.get('base_take_profit', self.BASE_TAKE_PROFIT)

        # 质量等级对应的参数调整系数
        self.tier_multipliers = {
            SignalQualityTier.EXCELLENT: {
                'position_mult': 1.6,    # 仓位 x1.6
                'stop_loss_mult': 0.75,  # 止损收紧 25%
                'take_profit_mult': 1.33, # 止盈放宽 33%
            },
            SignalQualityTier.GOOD: {
                'position_mult': 1.2,    # 仓位 x1.2
                'stop_loss_mult': 0.9,   # 止损收紧 10%
                'take_profit_mult': 1.1,  # 止盈放宽 10%
            },
            SignalQualityTier.FAIR: {
                'position_mult': 1.0,    # 基础仓位
                'stop_loss_mult': 1.0,   # 基础止损
                'take_profit_mult': 1.0,  # 基础止盈
            },
            SignalQualityTier.POOR: {
                'position_mult': 0.5,    # 仓位减半
                'stop_loss_mult': 1.25,  # 止损放宽 25%
                'take_profit_mult': 0.8,  # 止盈收紧 20%
            },
        }

        # 波动率调整系数
        self.volatility_sensitivity = self.config.get('volatility_sensitivity', 0.5)

        logger.info(
            f"动态仓位计算器已初始化 | "
            f"基础仓位={self.base_position_pct:.1%}, "
            f"最大仓位={self.max_position_pct:.1%}"
        )

    def calculate(self,
                 arb_score: float,
                 news_sentiment: float = 0.0,
                 volatility: float = 0.2,
                 base_position_pct: float = None) -> DynamicRiskParams:
        """
        计算动态风险参数

        Args:
            arb_score: 套利评分 (0-1)
            news_sentiment: 新闻情绪评分 (-1 到 1)
            volatility: 波动率 (0-1, 如 0.25 表示 25%)
            base_position_pct: 基础仓位 (可选，覆盖默认值)

        Returns:
            DynamicRiskParams: 动态风险参数
        """
        # 确定信号质量等级
        quality = self._assess_quality(arb_score)

        # 计算综合置信度
        confidence = self._calculate_confidence(arb_score, news_sentiment, volatility)

        # 获取调整系数
        mults = self.tier_multipliers[quality]

        # 计算仓位
        base_pos = base_position_pct or self.base_position_pct
        position_pct = base_pos * mults['position_mult']

        # 波动率调整 (高波动率降低仓位)
        if volatility > 0.3:
            vol_adjustment = 1.0 - (volatility - 0.3) * self.volatility_sensitivity
            position_pct *= max(0.5, vol_adjustment)

        # 仓位上限检查
        position_pct = min(position_pct, self.max_position_pct)

        # 计算止损
        stop_loss_pct = self.base_stop_loss * mults['stop_loss_mult']

        # 情绪调整 (负面情绪放宽止损)
        if news_sentiment < -0.3:
            stop_loss_pct *= 1.2  # 放宽 20%

        # 计算止盈
        take_profit_pct = self.base_take_profit * mults['take_profit_mult']

        # 情绪调整 (正面情绪放宽止盈)
        if news_sentiment > 0.3:
            take_profit_pct *= 1.2  # 放宽 20%

        # 计算追踪止损
        trailing_stop_pct = abs(stop_loss_pct) * 0.5  # 追踪止损为止损的一半

        # 持仓时间调整 (高质量信号允许更长持仓)
        if quality == SignalQualityTier.EXCELLENT:
            max_holding_minutes = 60
        elif quality == SignalQualityTier.GOOD:
            max_holding_minutes = 45
        else:
            max_holding_minutes = 30

        params = DynamicRiskParams(
            position_pct=round(position_pct, 4),
            max_position_pct=self.max_position_pct,
            stop_loss_pct=round(stop_loss_pct, 4),
            take_profit_pct=round(take_profit_pct, 4),
            trailing_stop_pct=round(trailing_stop_pct, 4),
            max_holding_minutes=max_holding_minutes,
            force_exit_time=self._calculate_force_exit_time(quality),
            max_slippage_pct=self._calculate_max_slippage(volatility),
            signal_quality=quality,
            confidence=round(confidence, 4),
            input_arb_score=arb_score,
            input_sentiment=news_sentiment,
            input_volatility=volatility,
        )

        logger.info(
            f"[动态仓位] arb={arb_score:.2f}, sentiment={news_sentiment:.2f}, "
            f"vol={volatility:.2f} → 仓位={position_pct:.1%}, "
            f"止损={stop_loss_pct:.1%}, 止盈={take_profit_pct:.1%}, "
            f"质量={quality.value}, 置信度={confidence:.2f}"
        )

        return params

    def _assess_quality(self, arb_score: float) -> SignalQualityTier:
        """评估信号质量等级"""
        if arb_score >= self.EXCELLENT_THRESHOLD:
            return SignalQualityTier.EXCELLENT
        elif arb_score >= self.GOOD_THRESHOLD:
            return SignalQualityTier.GOOD
        elif arb_score >= self.FAIR_THRESHOLD:
            return SignalQualityTier.FAIR
        else:
            return SignalQualityTier.POOR

    def _calculate_confidence(self,
                             arb_score: float,
                             news_sentiment: float,
                             volatility: float) -> float:
        """
        计算综合置信度

        逻辑:
        - arb_score 权重 50%
        - 情绪一致性 权重 30%
        - 低波动率 权重 20%
        """
        # arb_score 贡献
        arb_contrib = arb_score * 0.5

        # 情绪一致性贡献 (arb 和 sentiment 同向时加分)
        sentiment_aligned = (arb_score > 0.5 and news_sentiment > 0) or \
                           (arb_score < 0.5 and news_sentiment < 0)
        sentiment_contrib = (0.3 if sentiment_aligned else 0.1) * abs(news_sentiment)

        # 波动率贡献 (低波动率加分)
        vol_contrib = (1.0 - volatility) * 0.2

        confidence = arb_contrib + sentiment_contrib + vol_contrib
        return min(1.0, max(0.0, confidence))

    def _calculate_force_exit_time(self, quality: SignalQualityTier) -> str:
        """计算强制退出时间"""
        # 根据质量等级调整退出时间
        # 高质量信号允许更长的持仓时间
        if quality == SignalQualityTier.EXCELLENT:
            return "10:30"  # 允许持仓到 10:30
        elif quality == SignalQualityTier.GOOD:
            return "10:15"
        else:
            return "10:00"  # 默认 10:00 强制退出

    def _calculate_max_slippage(self, volatility: float) -> float:
        """计算最大滑点"""
        # 高波动率允许更大的滑点
        base = 0.005  # 0.5%
        adjustment = min(0.01, volatility * 0.02)
        return round(base + adjustment, 4)


# 全局单例
_sizer: Optional[DynamicPositionSizer] = None


def get_dynamic_positionSizer(config: Dict = None) -> DynamicPositionSizer:
    """获取全局 DynamicPositionSizer 单例"""
    global _sizer
    if _sizer is None:
        _sizer = DynamicPositionSizer(config=config)
    return _sizer


def reset_sizer():
    """重置计算器 (用于测试)"""
    global _sizer
    _sizer = None
