"""
市场状态检测器 (Market Regime Detector)

从 consensus_engine.py 提取的市场状态判定逻辑。
负责根据 ADX 值判断当前市场处于趋势/震荡/中性状态。

拆分自 consensus_engine.py 的 _get_market_regime 方法。
"""
import logging
from typing import Optional, Tuple, Dict

logger = logging.getLogger("ftseek.strategy.regime_detector")


class RegimeDetector:
    """
    市场状态检测器

    基于 ADX 判定市场状态:
      ADX >= adx_trend → trending (趋势市)
      ADX <= adx_range → ranging  (震荡市)
      中间值           → neutral  (中性)
    """

    def __init__(self, adx_trend: float = 25.0, adx_range: float = 20.0):
        self.adx_trend = adx_trend
        self.adx_range = adx_range

    def detect(self, code: str, adx_cache: Dict[str, float]) -> Tuple[str, Optional[float]]:
        """
        根据 ADX 判定市场状态

        Args:
            code: 股票代码
            adx_cache: {code: adx_value} 缓存

        Returns:
            (regime_str, adx_value)
            regime_str: "trending" / "ranging" / "neutral"
        """
        adx_val = adx_cache.get(code)
        if adx_val is None:
            return "neutral", None
        if adx_val >= self.adx_trend:
            return "trending", adx_val
        if adx_val <= self.adx_range:
            return "ranging", adx_val
        return "neutral", adx_val

    def classify_regime(self, adx_val: Optional[float]) -> str:
        """仅根据 ADX 值分类市场状态"""
        if adx_val is None:
            return "neutral"
        if adx_val >= self.adx_trend:
            return "trending"
        if adx_val <= self.adx_range:
            return "ranging"
        return "neutral"
