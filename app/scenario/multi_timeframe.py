"""
多周期特征提取器 (Multi-Timeframe Feature Extractor)

支持多周期 K 线数据的特征提取与对齐：
- 1 分钟/5 分钟/15 分钟/30 分钟/小时线
- 跨周期趋势确认
- 大周期制约小周期的权重调整

多周期共振原则:
1. 大周期趋势 > 小周期趋势
2. 多周期同向 = 高置信度
3. 多周期背离 = 降低置信度
"""
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime

from app.strategy.indicators import sma, ema, adx

logger = logging.getLogger("ftseek.scenario.multi_timeframe")
# 2026-04-10: propagate=True (默认), 通过 ftseek 父级 logger 输出


@dataclass
class TimeframeFeatures:
    """单个时间周期的特征"""
    timeframe: str = ""           # 周期名称 (1m, 5m, 15m, 1h)
    adx: float = 0.0              # ADX 值
    trend_direction: int = 0      # 趋势方向 (1=上涨，-1=下跌，0=震荡)
    ma_alignment: str = "neutral" # 均线排列
    price_vs_ma: float = 0.0      # 价格相对 MA 位置
    confidence: float = 0.5       # 该周期信号置信度


@dataclass
class MultiTimeframeFeatures:
    """多周期特征集合"""
    # 各周期特征
    m1: TimeframeFeatures = field(default_factory=TimeframeFeatures)
    m5: TimeframeFeatures = field(default_factory=TimeframeFeatures)
    m15: TimeframeFeatures = field(default_factory=TimeframeFeatures)
    m30: TimeframeFeatures = field(default_factory=TimeframeFeatures)
    h1: TimeframeFeatures = field(default_factory=TimeframeFeatures)

    # 跨周期分析
    dominant_timeframe: str = ""         # 主导周期
    trend_alignment: float = 0.0         # 趋势一致性 (-1 到 1)
    confidence_boost: float = 1.0        # 置信度提升倍数
    resonance_detected: bool = False     # 是否检测到多周期共振

    # 元数据
    code: str = ""
    timestamp: str = ""

    def to_dict(self) -> Dict:
        """转换为字典格式"""
        return {
            "dominant_timeframe": self.dominant_timeframe,
            "trend_alignment": round(self.trend_alignment, 2),
            "confidence_boost": round(self.confidence_boost, 2),
            "resonance_detected": self.resonance_detected,
            "timeframes": {
                tf: {"adx": getattr(self, tf).adx, "trend": getattr(self, tf).trend_direction}
                for tf in ['m1', 'm5', 'm15', 'm30', 'h1']
                if getattr(self, tf).timeframe
            }
        }


class MultiTimeframeFeatureExtractor:
    """
    多周期特征提取器

    功能:
    1. 从不同周期 K 线数据提取特征
    2. 跨周期趋势对齐分析
    3. 多周期共振检测
    4. 置信度调整
    """

    # 周期优先级 (从高到低)
    TIMEFRAME_PRIORITY = ['h1', 'm30', 'm15', 'm5', 'm1']

    # 周期权重 (用于加权平均)
    TIMEFRAME_WEIGHTS = {
        'h1': 1.0,    # 小时线权重最高
        'm30': 0.8,
        'm15': 0.6,
        'm5': 0.4,
        'm1': 0.2,
    }

    def __init__(self, config: Dict = None):
        """
        初始化多周期特征提取器

        Args:
            config: 配置字典
        """
        self.config = config or {}
        self.adx_period = self.config.get('adx_period', 14)
        self.ma_period = self.config.get('ma_period', 20)

        logger.info("多周期特征提取器已初始化")

    def extract(self, code: str, klines_by_timeframe: Dict[str, List[Dict]]) -> MultiTimeframeFeatures:
        """
        从多周期 K 线数据提取特征

        Args:
            code: 股票代码
            klines_by_timeframe: {timeframe: [klines]}

        Returns:
            MultiTimeframeFeatures: 多周期特征集合
        """
        result = MultiTimeframeFeatures(code=code, timestamp=datetime.now().isoformat())

        # 1. 提取各周期特征
        timeframe_features: Dict[str, TimeframeFeatures] = {}
        for tf, klines in klines_by_timeframe.items():
            if len(klines) < self.ma_period + 10:
                logger.debug(f"{code} {tf} K 线数据不足")
                continue

            tf_features = self._extract_single_timeframe(tf, klines)
            timeframe_features[tf] = tf_features
            setattr(result, tf, tf_features)

        # 2. 跨周期分析
        if timeframe_features:
            result.dominant_timeframe = self._find_dominant_timeframe(timeframe_features)
            result.trend_alignment = self._calculate_trend_alignment(timeframe_features)
            result.confidence_boost = self._calculate_confidence_boost(timeframe_features)
            result.resonance_detected = self._check_resonance(timeframe_features)

        logger.info(
            f"[{code}] 多周期分析：主导周期={result.dominant_timeframe} | "
            f"趋势一致性={result.trend_alignment:.2f} | "
            f"共振={result.resonance_detected}"
        )

        return result

    def _extract_single_timeframe(self, timeframe: str, klines: List[Dict]) -> TimeframeFeatures:
        """
        提取单个时间周期的特征

        Args:
            timeframe: 周期名称
            klines: K 线数据列表

        Returns:
            TimeframeFeatures: 单周期特征
        """
        closes = [k['close'] for k in klines]
        highs = [k['high'] for k in klines]
        lows = [k['low'] for k in klines]

        # ADX
        adx_value = adx(highs, lows, closes, self.adx_period) or 0.0

        # MA 排列
        ma_fast = ema(closes, 10) if len(closes) >= 10 else closes[-1]
        ma_mid = sma(closes, 20) if len(closes) >= 20 else closes[-1]
        ma_slow = sma(closes, 50) if len(closes) >= 50 else closes[-1]

        if ma_fast > ma_mid > ma_slow:
            ma_alignment = "bullish"
            trend_direction = 1
        elif ma_fast < ma_mid < ma_slow:
            ma_alignment = "bearish"
            trend_direction = -1
        else:
            ma_alignment = "neutral"
            trend_direction = 0

        # 价格相对 MA 位置
        ma_value = sma(closes, self.ma_period) if len(closes) >= self.ma_period else closes[-1]
        price_vs_ma = (closes[-1] - ma_value) / ma_value if ma_value else 0.0

        # 置信度 (基于 ADX)
        if adx_value >= 30:
            confidence = min(0.9, 0.6 + (adx_value - 30) / 100)
        elif adx_value >= 20:
            confidence = 0.5 + (adx_value - 20) / 100
        else:
            confidence = 0.3 + adx_value / 100

        return TimeframeFeatures(
            timeframe=timeframe,
            adx=round(adx_value, 2),
            trend_direction=trend_direction,
            ma_alignment=ma_alignment,
            price_vs_ma=round(price_vs_ma, 4),
            confidence=round(confidence, 2),
        )

    def _find_dominant_timeframe(self, features: Dict[str, TimeframeFeatures]) -> str:
        """
        找出主导周期

        原则:
        1. ADX 最高的周期
        2. 大周期优先 (同 ADX 时)
        """
        if not features:
            return ""

        # 按 ADX 排序，考虑周期权重
        scored = []
        for tf, feat in features.items():
            weight = self.TIMEFRAME_WEIGHTS.get(tf, 0.5)
            score = feat.adx * weight
            scored.append((tf, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[0][0] if scored else ""

    def _calculate_trend_alignment(self, features: Dict[str, TimeframeFeatures]) -> float:
        """
        计算趋势一致性

        Returns:
            float: -1(完全背离) 到 1(完全一致)
        """
        if len(features) < 2:
            return 0.0

        directions = [f.trend_direction for f in features.values() if f.trend_direction != 0]
        if not directions:
            return 0.0

        # 计算方向一致性
        bullish = sum(1 for d in directions if d > 0)
        bearish = sum(1 for d in directions if d < 0)
        total = len(directions)

        if bullish == total:
            return 1.0  # 全部看涨
        elif bearish == total:
            return -1.0  # 全部看跌
        else:
            # 部分一致
            max_side = max(bullish, bearish)
            return (max_side - (total - max_side)) / total

    def _calculate_confidence_boost(self, features: Dict[str, TimeframeFeatures]) -> float:
        """
        计算置信度提升倍数

        原则:
        1. 多周期同向 → 提升置信度
        2. 大周期确认小周期 → 提升
        3. 多周期背离 → 降低
        """
        if len(features) < 2:
            return 1.0

        alignment = self._calculate_trend_alignment(features)

        # 完全一致 → 1.5x 置信度
        if abs(alignment) >= 0.9:
            return 1.5
        # 大部分一致 → 1.2x
        elif abs(alignment) >= 0.5:
            return 1.2
        # 背离 → 0.7x
        elif abs(alignment) <= 0.3:
            return 0.7
        else:
            return 1.0

    def _check_resonance(self, features: Dict[str, TimeframeFeatures]) -> bool:
        """
        检查多周期共振

        共振条件:
        1. 至少 3 个周期
        2. 趋势一致性 >= 0.8
        3. 主导周期 ADX >= 25
        """
        if len(features) < 3:
            return False

        alignment = self._calculate_trend_alignment(features)
        if abs(alignment) < 0.8:
            return False

        dominant_tf = self._find_dominant_timeframe(features)
        dominant_feat = features.get(dominant_tf)
        if not dominant_feat or dominant_feat.adx < 25:
            return False

        return True

    def get_scenario_adjustment(self, base_scenario: str, base_confidence: float) -> Tuple[str, float]:
        """
        基于多周期分析调整场景和置信度

        Args:
            base_scenario: 基础场景
            base_confidence: 基础置信度

        Returns:
            (adjusted_scenario, adjusted_confidence)
        """
        # 此方法需要与场景分类器集成
        # 这里提供基础框架
        return base_scenario, base_confidence


# 全局单例
_extractor: Optional[MultiTimeframeFeatureExtractor] = None


def get_multi_timeframe_extractor(config: Dict = None) -> MultiTimeframeFeatureExtractor:
    """获取多周期特征提取器单例"""
    global _extractor
    if _extractor is None:
        _extractor = MultiTimeframeFeatureExtractor(config)
    return _extractor
