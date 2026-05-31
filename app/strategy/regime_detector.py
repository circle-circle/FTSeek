"""
Market Regime 检测器 — 基于 VIX + SPY 相对均线位置判断市场状态

输出 regime 类型:
- trending_up: SPY > MA50 > MA200，上升趋势
- trending_down: SPY < MA50 < MA200，下跌趋势
- range_bound: SPY 在 MA50 和 MA200 之间震荡
- high_vol: VIX > 30，高波动（无论方向）
- low_vol: VIX < 15，低波动平静

Task #8 (2026-05-31): 配合 ConsensusEngine.REGIME_WEIGHTS 实现策略权重的市场态自适应切换。
"""
import logging
from typing import Dict

logger = logging.getLogger("ftseek.regime_detector")


class MarketRegimeDetector:
    """基于宏观指标检测当前市场状态"""

    # VIX 分界线
    VIX_EXTREME = 40
    VIX_HIGH = 30
    VIX_ELEVATED = 20
    VIX_LOW = 15

    def detect(self, market_context: Dict) -> str:
        """
        检测当前市场 regime

        Args:
            market_context: 含 vix, spy_price, spy_ma_20, spy_ma_50, spy_ma_200 等字段

        Returns:
            regime 字符串
        """
        vix = float(market_context.get("vix") or 20)
        spy_price = float(market_context.get("spy_price") or 0)
        spy_ma_50 = float(market_context.get("spy_ma_50") or 0)
        spy_ma_200 = float(market_context.get("spy_ma_200") or 0)

        # VIX 优先级最高
        if vix >= self.VIX_EXTREME:
            return "crisis"
        if vix >= self.VIX_HIGH:
            # 高波动 + 方向判断
            trend = self._detect_trend(spy_price, spy_ma_50, spy_ma_200)
            if trend == "up":
                return "trending_up_high_vol"
            elif trend == "down":
                return "trending_down_high_vol"
            return "high_vol"

        # 常规 VIX 下的趋势判断
        trend = self._detect_trend(spy_price, spy_ma_50, spy_ma_200)

        if vix <= self.VIX_LOW:
            if trend == "up":
                return "trending_up"
            return "low_vol"

        # VIX 15-30 范围
        if trend == "up":
            return "trending_up"
        elif trend == "down":
            return "trending_down"
        return "range_bound"

    def _detect_trend(self, price: float, ma50: float, ma200: float) -> str:
        """基于均线位置判断趋势方向"""
        if not ma50 or not ma200 or not price:
            return "unknown"

        if price > ma50 > ma200:
            return "up"
        elif price < ma50 < ma200:
            return "down"
        else:
            return "range"
