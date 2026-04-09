"""
市场状态适配器 (Regime Adapter)

功能:
- 评估策略与市场状态的匹配度
- 不同市场状态下策略的表现适配
- 动态调整策略权重
"""

import logging
from typing import Dict, Optional

logger = logging.getLogger("ftseek.quality.regime_adapter")
logger.propagate = False


# 策略类型定义
# 趋势型策略：在趋势行情中表现更好
TREND_STRATEGIES = {"supertrend", "macd", "ma_cross", "price_break"}

# 震荡型策略：在盘整行情中表现更好
OSCILLATOR_STRATEGIES = {"rsi", "bollinger", "volume_alert"}

# 综合型策略：多因子融合，任何行情都有参考价值
HYBRID_STRATEGIES = {"confluence", "confluence_v2", "fusion_v2", "fusion_v3"}

# 情绪型策略：基于新闻和情绪分析
SENTIMENT_STRATEGIES = {"sentiment", "sentiment_ai", "news_sentiment"}

# 开盘/收盘效应策略 (事件驱动型)
EVENT_DRIVEN_STRATEGIES = {"open_close_effect"}


# 市场状态定义
# strong_trend_up: 强上涨趋势 (ADX > 30, 价格 > MA30)
# weak_trend_up: 弱上涨趋势 (ADX 20-30, 价格 > MA30)
# range_bound: 震荡市 (ADX < 20)
# weak_trend_down: 弱下跌趋势 (ADX 20-30, 价格 < MA30)
# strong_trend_down: 强下跌趋势 (ADX > 30, 价格 < MA30)
# high_volatility: 高波动市场 (ATR 百分位 > 80)


# 策略 - 市场适配度矩阵
# 值域：0.0 (完全不适配) 到 1.0 (完全适配)
REGIME_FIT_MATRIX: Dict[str, Dict[str, float]] = {
    # 趋势型策略
    "supertrend": {
        "strong_trend_up": 0.95,      # 强上涨：非常适合
        "weak_trend_up": 0.75,        # 弱上涨：适合
        "range_bound": 0.35,          # 震荡：不适合
        "weak_trend_down": 0.70,      # 弱下跌：适合（可反向）
        "strong_trend_down": 0.90,    # 强下跌：非常适合（可反向）
        "high_volatility": 0.60,      # 高波动：中等
        "default": 0.60,
    },
    "macd": {
        "strong_trend_up": 0.85,
        "weak_trend_up": 0.70,
        "range_bound": 0.40,
        "weak_trend_down": 0.65,
        "strong_trend_down": 0.80,
        "high_volatility": 0.55,
        "default": 0.55,
    },
    "ma_cross": {
        "strong_trend_up": 0.90,
        "weak_trend_up": 0.70,
        "range_bound": 0.30,
        "weak_trend_down": 0.65,
        "strong_trend_down": 0.85,
        "high_volatility": 0.50,
        "default": 0.55,
    },
    "price_break": {
        "strong_trend_up": 0.80,
        "weak_trend_up": 0.65,
        "range_bound": 0.45,
        "weak_trend_down": 0.60,
        "strong_trend_down": 0.75,
        "high_volatility": 0.70,
        "default": 0.60,
    },

    # 震荡型策略
    "rsi": {
        "strong_trend_up": 0.45,      # 趋势市：容易超买钝化
        "weak_trend_up": 0.55,
        "range_bound": 0.90,          # 震荡市：非常适合
        "weak_trend_down": 0.50,
        "strong_trend_down": 0.40,    # 强下跌：容易超卖钝化
        "high_volatility": 0.50,
        "default": 0.55,
    },
    "bollinger": {
        "strong_trend_up": 0.50,
        "weak_trend_up": 0.60,
        "range_bound": 0.85,
        "weak_trend_down": 0.55,
        "strong_trend_down": 0.45,
        "high_volatility": 0.65,      # 高波动：布林带扩张，有参考价值
        "default": 0.55,
    },
    "volume_alert": {
        "strong_trend_up": 0.60,
        "weak_trend_up": 0.65,
        "range_bound": 0.75,
        "weak_trend_down": 0.60,
        "strong_trend_down": 0.55,
        "high_volatility": 0.70,
        "default": 0.60,
    },

    # 综合型策略
    "confluence": {
        "strong_trend_up": 0.80,
        "weak_trend_up": 0.75,
        "range_bound": 0.70,
        "weak_trend_down": 0.70,
        "strong_trend_down": 0.75,
        "high_volatility": 0.65,
        "default": 0.75,
    },
    "confluence_v2": {
        "strong_trend_up": 0.85,      # V2 版本有市场状态感知，适配度更高
        "weak_trend_up": 0.80,
        "range_bound": 0.75,
        "weak_trend_down": 0.75,
        "strong_trend_down": 0.80,
        "high_volatility": 0.70,
        "default": 0.80,
    },
    "fusion_v2": {
        "strong_trend_up": 0.85,
        "weak_trend_up": 0.80,
        "range_bound": 0.75,
        "weak_trend_down": 0.75,
        "strong_trend_down": 0.80,
        "high_volatility": 0.70,
        "default": 0.80,
    },
    "fusion_v3": {
        "strong_trend_up": 0.88,
        "weak_trend_up": 0.82,
        "range_bound": 0.78,
        "weak_trend_down": 0.78,
        "strong_trend_down": 0.82,
        "high_volatility": 0.72,
        "default": 0.82,
    },

    # 情绪型策略
    "sentiment": {
        "strong_trend_up": 0.70,
        "weak_trend_up": 0.65,
        "range_bound": 0.60,
        "weak_trend_down": 0.60,
        "strong_trend_down": 0.65,
        "high_volatility": 0.75,     # 高波动时情绪影响更大
        "default": 0.65,
    },
    "sentiment_ai": {
        "strong_trend_up": 0.72,
        "weak_trend_up": 0.68,
        "range_bound": 0.62,
        "weak_trend_down": 0.62,
        "strong_trend_down": 0.68,
        "high_volatility": 0.78,
        "default": 0.68,
    },
    "news_sentiment": {
        "strong_trend_up": 0.70,
        "weak_trend_up": 0.65,
        "range_bound": 0.60,
        "weak_trend_down": 0.60,
        "strong_trend_down": 0.65,
        "high_volatility": 0.75,
        "default": 0.65,
    },

    # 事件驱动型策略
    "open_close_effect": {
        "strong_trend_up": 0.75,     # 趋势日开盘突破成功率更高
        "weak_trend_up": 0.70,
        "range_bound": 0.55,         # 震荡市开盘突破可能失败
        "weak_trend_down": 0.60,
        "strong_trend_down": 0.65,
        "high_volatility": 0.70,
        "default": 0.65,
    },
}


class RegimeAdapter:
    """
    市场状态适配器

    根据策略类型和市场状态，计算适配度得分
    """

    def __init__(self, custom_fit_matrix: Optional[Dict] = None):
        """
        初始化适配器

        Args:
            custom_fit_matrix: 自定义适配度矩阵，覆盖默认值
        """
        self.fit_matrix = REGIME_FIT_MATRIX.copy()

        # 合并自定义配置
        if custom_fit_matrix:
            for strategy, regimes in custom_fit_matrix.items():
                if strategy not in self.fit_matrix:
                    self.fit_matrix[strategy] = {}
                self.fit_matrix[strategy].update(regimes)

        logger.info("市场状态适配器已初始化")

    def get_fit_score(self, strategy: str, market_regime: str) -> float:
        """
        获取策略 - 市场适配度得分

        Args:
            strategy: 策略名称
            market_regime: 市场状态

        Returns:
            适配度得分 (0-1)
        """
        # 获取策略的适配度配置
        strategy_regimes = self.fit_matrix.get(strategy, {})

        if not strategy_regimes:
            # 未知策略，根据策略类型返回默认值
            return self._get_type_default_fit(strategy, market_regime)

        # 获取具体市场状态的适配度
        fit_score = strategy_regimes.get(market_regime)

        if fit_score is None:
            # 未知市场状态，返回默认值
            fit_score = strategy_regimes.get("default", 0.60)

        return fit_score

    def _get_type_default_fit(self, strategy: str, market_regime: str) -> float:
        """
        根据策略类型返回默认适配度

        Args:
            strategy: 策略名称
            market_regime: 市场状态

        Returns:
            默认适配度得分
        """
        # 判断策略类型
        strategy_lower = strategy.lower()

        if strategy_lower in TREND_STRATEGIES:
            return self._get_trend_strategy_fit(market_regime)
        elif strategy_lower in OSCILLATOR_STRATEGIES:
            return self._get_oscillator_strategy_fit(market_regime)
        elif strategy_lower in HYBRID_STRATEGIES:
            return self._get_hybrid_strategy_fit(market_regime)
        elif strategy_lower in SENTIMENT_STRATEGIES:
            return self._get_sentiment_strategy_fit(market_regime)
        elif strategy_lower in EVENT_DRIVEN_STRATEGIES:
            return self._get_event_driven_fit(market_regime)
        else:
            # 未知类型，返回中性值
            return 0.60

    def _get_trend_strategy_fit(self, market_regime: str) -> float:
        """趋势型策略适配度"""
        fits = {
            "strong_trend_up": 0.90,
            "weak_trend_up": 0.70,
            "range_bound": 0.35,
            "weak_trend_down": 0.65,
            "strong_trend_down": 0.85,
            "high_volatility": 0.55,
            "default": 0.55,
        }
        return fits.get(market_regime, fits["default"])

    def _get_oscillator_strategy_fit(self, market_regime: str) -> float:
        """震荡型策略适配度"""
        fits = {
            "strong_trend_up": 0.45,
            "weak_trend_up": 0.55,
            "range_bound": 0.85,
            "weak_trend_down": 0.50,
            "strong_trend_down": 0.40,
            "high_volatility": 0.50,
            "default": 0.55,
        }
        return fits.get(market_regime, fits["default"])

    def _get_hybrid_strategy_fit(self, market_regime: str) -> float:
        """综合型策略适配度"""
        fits = {
            "strong_trend_up": 0.80,
            "weak_trend_up": 0.75,
            "range_bound": 0.70,
            "weak_trend_down": 0.70,
            "strong_trend_down": 0.75,
            "high_volatility": 0.65,
            "default": 0.75,
        }
        return fits.get(market_regime, fits["default"])

    def _get_sentiment_strategy_fit(self, market_regime: str) -> float:
        """情绪型策略适配度"""
        fits = {
            "strong_trend_up": 0.70,
            "weak_trend_up": 0.65,
            "range_bound": 0.60,
            "weak_trend_down": 0.60,
            "strong_trend_down": 0.65,
            "high_volatility": 0.75,
            "default": 0.65,
        }
        return fits.get(market_regime, fits["default"])

    def _get_event_driven_fit(self, market_regime: str) -> float:
        """事件驱动型策略适配度"""
        fits = {
            "strong_trend_up": 0.75,
            "weak_trend_up": 0.70,
            "range_bound": 0.55,
            "weak_trend_down": 0.60,
            "strong_trend_down": 0.65,
            "high_volatility": 0.70,
            "default": 0.65,
        }
        return fits.get(market_regime, fits["default"])

    def get_best_regime(self, strategy: str) -> str:
        """
        获取策略最适合的市场状态

        Args:
            strategy: 策略名称

        Returns:
            最适合的市场状态
        """
        strategy_regimes = self.fit_matrix.get(strategy, {})

        if not strategy_regimes:
            # 未知策略，根据类型判断
            strategy_lower = strategy.lower()
            if strategy_lower in TREND_STRATEGIES:
                return "strong_trend_up"
            elif strategy_lower in OSCILLATOR_STRATEGIES:
                return "range_bound"
            else:
                return "default"

        # 找出适配度最高的市场状态
        best_regime = "default"
        best_score = 0.0

        for regime, score in strategy_regimes.items():
            if regime != "default" and score > best_score:
                best_score = score
                best_regime = regime

        return best_regime

    def get_worst_regime(self, strategy: str) -> str:
        """
        获取策略最不适合的市场状态

        Args:
            strategy: 策略名称

        Returns:
            最不适合的市场状态
        """
        strategy_regimes = self.fit_matrix.get(strategy, {})

        if not strategy_regimes:
            return "range_bound" if strategy.lower() in TREND_STRATEGIES else "strong_trend_up"

        # 找出适配度最低的市场状态
        worst_regime = "default"
        worst_score = 1.0

        for regime, score in strategy_regimes.items():
            if regime != "default" and score < worst_score:
                worst_score = score
                worst_regime = regime

        return worst_regime

    def should_boost_strategy(self, strategy: str, market_regime: str,
                               threshold: float = 0.75) -> bool:
        """
        判断是否应该提升策略权重

        Args:
            strategy: 策略名称
            market_regime: 市场状态
            threshold: 适配度阈值

        Returns:
            True 如果应该提升权重
        """
        fit_score = self.get_fit_score(strategy, market_regime)
        return fit_score >= threshold

    def should_decay_strategy(self, strategy: str, market_regime: str,
                               threshold: float = 0.50) -> bool:
        """
        判断是否应该降低策略权重

        Args:
            strategy: 策略名称
            market_regime: 市场状态
            threshold: 适配度阈值

        Returns:
            True 如果应该降低权重
        """
        fit_score = self.get_fit_score(strategy, market_regime)
        return fit_score < threshold
