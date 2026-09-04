"""
价格突破策略

策略逻辑：
- 当价格突破阻力位且成交量放大时，生成做多信号
- 当价格跌破支撑位且成交量放大时，生成做空信号

P1 模块化 (2026-04-06): 从 signal_generator.py 迁移至此
"""
from typing import Dict, Any, List
from .base import BaseStrategy, StrategyResult, register_strategy


@register_strategy
class PriceBreakStrategy(BaseStrategy):
    """价格突破策略"""

    @property
    def name(self) -> str:
        return "price_break"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "价格突破策略：突破阻力/支撑位 + 成交量确认"

    @property
    def required_data(self) -> List[str]:
        return ["current_price", "resistance", "support", "volume_ratio"]

    def generate_signal(self, symbol: str, market_data: Dict[str, Any]) -> StrategyResult:
        """
        生成价格突破信号

        Args:
            symbol: 股票代码
            market_data: 市场数据（需包含 current_price, resistance, support, volume_ratio）

        Returns:
            StrategyResult: 策略结果
        """
        current_price = market_data.get("current_price", 0)
        resistance = market_data.get("resistance", 0)
        support = market_data.get("support", 0)
        volume_ratio = market_data.get("volume_ratio", 1.0)

        # 突破阻力位 + 放量 → 做多
        # DEB-20260903 #73: resistance<=0 时 (current_price-resistance)/resistance
        # 除零崩溃, 且 current_price>0>resistance 恒真 → 假突破信号; 无效价位跳过
        if resistance > 0 and current_price > resistance and volume_ratio > 1.5:
            # 计算强度：突破幅度越大，强度越高
            breakout_ratio = (current_price - resistance) / resistance
            strength = min(100, 60 + int(breakout_ratio * 100))

            # 计算置信度：成交量越大，置信度越高
            volume_factor = min(1.0, volume_ratio / 3.0)
            confidence = self._calculate_confidence({
                "breakout": 0.8,
                "volume_confirm": volume_factor,
            })

            return StrategyResult(
                direction="LONG",
                strength=strength,
                confidence=confidence,
                expected_horizon=120,  # 2 小时
                volatility_fit=True,
                market_regime="TREND_UP",
                risk_reward=2.5,
                score_breakdown={
                    "breakout": 30,
                    "volume_confirm": 20,
                    "trend_align": 20,
                },
                metadata={
                    "breakout_ratio": breakout_ratio,
                    "volume_ratio": volume_ratio,
                }
            )

        # 跌破支撑位 + 放量 → 做空
        # DEB-20260903 #73: support<=0 时除零崩溃, 无效价位跳过
        if support > 0 and current_price < support and volume_ratio > 1.5:
            # 计算强度：跌破幅度越大，强度越高
            breakdown_ratio = (support - current_price) / support
            # DEB-20260903 #73: 原 max(0, 40 - int(...)) 使跌破越深强度越低,
            # 与注释相反; 改为随跌破幅度递增 (上限 100)
            strength = min(100, 40 + int(breakdown_ratio * 100))

            # 计算置信度
            volume_factor = min(1.0, volume_ratio / 3.0)
            confidence = self._calculate_confidence({
                "breakdown": 0.8,
                "volume_confirm": volume_factor,
            })

            return StrategyResult(
                direction="SHORT",
                strength=strength,
                confidence=confidence,
                expected_horizon=120,
                volatility_fit=True,
                market_regime="TREND_DOWN",
                risk_reward=2.5,
                score_breakdown={
                    "breakdown": 30,
                    "volume_confirm": 20,
                    "trend_align": 20,
                },
                metadata={
                    "breakdown_ratio": breakdown_ratio,
                    "volume_ratio": volume_ratio,
                }
            )

        # 无信号
        return StrategyResult()
