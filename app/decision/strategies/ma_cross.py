"""
均线交叉策略 (MA Cross)

策略逻辑：
- 当短期均线上穿长期均线时，生成做多信号（金叉）
- 当短期均线下穿长期均线时，生成做空信号（死叉）

P1 模块化 (2026-04-06): 新增策略模块
"""
from typing import Dict, Any, List
from .base import BaseStrategy, StrategyResult, register_strategy


@register_strategy
class MACrossStrategy(BaseStrategy):
    """均线交叉策略"""

    @property
    def name(self) -> str:
        return "ma_cross"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "均线交叉策略：金叉做多，死叉做空"

    @property
    def required_data(self) -> List[str]:
        return ["ma5", "ma10", "ma20", "ma50", "current_price"]

    def generate_signal(self, symbol: str, market_data: Dict[str, Any]) -> StrategyResult:
        """
        生成均线交叉信号

        Args:
            symbol: 股票代码
            market_data: 市场数据（需包含 MA5, MA10, MA20, MA50）

        Returns:
            StrategyResult: 策略结果
        """
        ma5 = market_data.get("ma5", 0)
        ma10 = market_data.get("ma10", 0)
        ma20 = market_data.get("ma20", 0)
        ma50 = market_data.get("ma50", 0)
        price = market_data.get("current_price", 0)

        if not all([ma5, ma10, ma20, ma50, price]):
            return StrategyResult()

        # 金叉检测：MA5 上穿 MA10 或 MA20
        golden_cross = (ma5 > ma10 and ma5 > ma20)
        # 多头排列：MA5 > MA10 > MA20 > MA50
        bullish_order = (ma5 > ma10 > ma20 > ma50)
        # 价格位置：价格 > MA5
        price_above = price > ma5

        # 死叉检测：MA5 下穿 MA10 或 MA20
        death_cross = (ma5 < ma10 and ma5 < ma20)
        # 空头排列：MA5 < MA10 < MA20 < MA50
        bearish_order = (ma5 < ma10 < ma20 < ma50)
        # 价格位置：价格 < MA5
        price_below = price < ma5

        # 金叉 + 多头排列 → 做多
        if golden_cross and bullish_order:
            # 计算强度：排列越整齐，强度越高
            alignment_score = 0
            if ma5 > ma10: alignment_score += 25
            if ma10 > ma20: alignment_score += 25
            if ma20 > ma50: alignment_score += 25
            if price_above: alignment_score += 25

            strength = 50 + int(alignment_score / 100 * 40)  # 50-90

            confidence = self._calculate_confidence({
                "cross_confirm": 0.8 if golden_cross else 0.5,
                "alignment": alignment_score / 100,
                "price_position": 0.8 if price_above else 0.3,
            })

            return StrategyResult(
                direction="LONG",
                strength=strength,
                confidence=confidence,
                expected_horizon=240,  # 4 小时
                volatility_fit=True,
                market_regime="TREND_UP",
                risk_reward=2.0,
                score_breakdown={
                    "cross_signal": 30,
                    "alignment": 25,
                    "price_confirm": 15,
                },
                metadata={
                    "golden_cross": golden_cross,
                    "bullish_order": bullish_order,
                    "alignment_score": alignment_score,
                }
            )

        # 死叉 + 空头排列 → 做空
        if death_cross and bearish_order:
            # 计算强度：排列越整齐，强度越高
            alignment_score = 0
            if ma5 < ma10: alignment_score += 25
            if ma10 < ma20: alignment_score += 25
            if ma20 < ma50: alignment_score += 25
            if price_below: alignment_score += 25

            strength = 50 - int(alignment_score / 100 * 40)  # 10-50

            confidence = self._calculate_confidence({
                "cross_confirm": 0.8 if death_cross else 0.5,
                "alignment": alignment_score / 100,
                "price_position": 0.8 if price_below else 0.3,
            })

            return StrategyResult(
                direction="SHORT",
                strength=strength,
                confidence=confidence,
                expected_horizon=240,
                volatility_fit=True,
                market_regime="TREND_DOWN",
                risk_reward=2.0,
                score_breakdown={
                    "cross_signal": 30,
                    "alignment": 25,
                    "price_confirm": 15,
                },
                metadata={
                    "death_cross": death_cross,
                    "bearish_order": bearish_order,
                    "alignment_score": alignment_score,
                }
            )

        # 无信号
        return StrategyResult()
