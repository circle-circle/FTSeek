"""
RSI 反弹策略

策略逻辑：
- 当 RSI 超卖（<30）时，生成做多信号（反弹）
- 当 RSI 超买（>70）时，生成做空信号（回调）

P1 模块化 (2026-04-06): 新增策略模块
"""
from typing import Dict, Any, List
from .base import BaseStrategy, StrategyResult, register_strategy


@register_strategy
class RSIReboundStrategy(BaseStrategy):
    """RSI 反弹策略"""

    @property
    def name(self) -> str:
        return "rsi_rebound"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "RSI 策略：超卖反弹做多，超买回调做空"

    @property
    def required_data(self) -> List[str]:
        return ["rsi", "current_price"]

    def generate_signal(self, symbol: str, market_data: Dict[str, Any]) -> StrategyResult:
        """
        生成 RSI 信号

        Args:
            symbol: 股票代码
            market_data: 市场数据（需包含 RSI）

        Returns:
            StrategyResult: 策略结果
        """
        rsi = market_data.get("rsi", 50)
        price = market_data.get("current_price", 0)

        # DEB-20260904 #168: RSI=0 是合法极端超卖读数, `not rsi` 会把它当缺数据
        # 丢弃最强的超卖信号 — 仅 None 视为缺失
        if rsi is None or not price:
            return StrategyResult()

        # RSI 超卖 → 做多（反弹）
        if rsi < 30:
            # 计算强度：RSI 越低，强度越高
            # DEB-20260903 #74: 原公式 70 - int((30-rsi)*2) 与注释相反 (RSI=10→30, RSI=29→68,
            #   超卖越严重信号越弱). 改为随超卖加深递增, 上限 100.
            strength = min(100, max(10, 70 + int((30 - rsi) * 2)))

            # 计算置信度：超卖越严重，置信度越高
            oversold_factor = min(1.0, (30 - rsi) / 20)
            confidence = self._calculate_confidence({
                "oversold": oversold_factor,
                "mean_reversion": 0.7,
            })

            return StrategyResult(
                direction="LONG",
                strength=strength,
                confidence=confidence,
                expected_horizon=180,  # 3 小时
                volatility_fit=True,
                market_regime="RANGE",
                risk_reward=2.5,
                score_breakdown={
                    "oversold_signal": 35,
                    "mean_reversion": 20,
                    "volume_confirm": 15,
                },
                metadata={
                    "rsi": rsi,
                    "signal_type": "oversold_rebound",
                }
            )

        # RSI 超买 → 做空（回调）
        if rsi > 70:
            # 计算强度：RSI 越高，强度越高（strength 越低）
            strength = min(90, 50 + int((rsi - 70) * 2))

            # 计算置信度
            overbought_factor = min(1.0, (rsi - 70) / 20)
            confidence = self._calculate_confidence({
                "overbought": overbought_factor,
                "mean_reversion": 0.7,
            })

            return StrategyResult(
                direction="SHORT",
                strength=strength,
                confidence=confidence,
                expected_horizon=180,
                volatility_fit=True,
                market_regime="RANGE",
                risk_reward=2.5,
                score_breakdown={
                    "overbought_signal": 35,
                    "mean_reversion": 20,
                    "volume_confirm": 15,
                },
                metadata={
                    "rsi": rsi,
                    "signal_type": "overbought_pullback",
                }
            )

        # RSI 中性区域 → 无信号
        return StrategyResult()
