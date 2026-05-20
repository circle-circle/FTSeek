"""
市场状态识别模块 (Market Regime Detection)

基于多个指标综合判断当前市场状态，为策略选择提供依据

市场状态分类:
- STRONG_TREND_UP: 强上涨趋势 (ADX > 30, 价格在均线上方)
- WEAK_TREND_UP: 弱上涨趋势 (ADX 20-30, 价格在均线上方)
- RANGE_BOUND: 震荡市 (ADX < 20)
- WEAK_TREND_DOWN: 弱下跌趋势 (ADX 20-30, 价格在均线下方)
- STRONG_TREND_DOWN: 强下跌趋势 (ADX > 30, 价格在均线下方)
- HIGH_VOLATILITY: 高波动 (ATR 百分位 > 80)
- LOW_VOLATILITY: 低波动 (ATR 百分位 < 20)
"""
import logging
from enum import Enum
from typing import Dict, Optional, Tuple, List
from dataclasses import dataclass
from collections import deque

from .indicators import adx, atr, sma, ema, bollinger_bands

logger = logging.getLogger("ftseek.strategy.market_regime")
logger.propagate = False  # 防止日志重复


class TrendDirection(Enum):
    """趋势方向"""
    STRONG_UP = "strong_up"
    WEAK_UP = "weak_up"
    NEUTRAL = "neutral"
    WEAK_DOWN = "weak_down"
    STRONG_DOWN = "strong_down"


class VolatilityState(Enum):
    """波动率状态"""
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


class MarketRegimeType(Enum):
    """市场体制类型"""
    STRONG_TREND_UP = "strong_trend_up"
    WEAK_TREND_UP = "weak_trend_up"
    RANGE_BOUND = "range_bound"
    WEAK_TREND_DOWN = "weak_trend_down"
    STRONG_TREND_DOWN = "strong_trend_down"
    HIGH_VOLATILITY = "high_volatility"
    LOW_VOLATILITY = "low_volatility"


@dataclass
class MarketRegime:
    """市场状态数据类"""
    code: str
    regime_type: MarketRegimeType
    trend_direction: TrendDirection
    volatility_state: VolatilityState
    adx_value: float
    atr_value: float
    atr_percentile: float
    price_vs_ma: float  # 价格相对 MA 的百分比
    bb_width: float  # 布林带宽度
    confidence: float  # 判断置信度 (0-1)
    timestamp: float

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "regime": self.regime_type.value,
            "trend": self.trend_direction.value,
            "volatility": self.volatility_state.value,
            "adx": round(self.adx_value, 2),
            "atr": round(self.atr_value, 4),
            "atr_percentile": round(self.atr_percentile, 2),
            "price_vs_ma": round(self.price_vs_ma, 4),
            "bb_width": round(self.bb_width, 4),
            "confidence": round(self.confidence, 2),
        }


class MarketRegimeDetector:
    """
    市场状态检测器
    
    使用多指标融合判断市场状态:
    1. ADX - 趋势强度
    2. 价格 vs MA - 趋势方向
    3. ATR 百分位 - 波动率状态
    4. 布林带宽度 - 波动率确认
    """

    # 配置参数
    ADX_STRONG_THRESHOLD = 30.0  # 强趋势阈值
    ADX_WEAK_THRESHOLD = 20.0    # 弱趋势阈值
    ATR_HIGH_PERCENTILE = 80.0   # 高波动百分位
    ATR_LOW_PERCENTILE = 20.0    # 低波动百分位
    MA_PERIOD = 50               # 趋势判断均线周期
    ATR_LOOKBACK = 100           # ATR 百分位计算周期

    def __init__(self, config: dict = None):
        """
        初始化检测器
        
        Args:
            config: 配置字典，可覆盖默认阈值
        """
        self.config = config or {}
        self.ADX_STRONG_THRESHOLD = self.config.get("adx_strong", 30.0)
        self.ADX_WEAK_THRESHOLD = self.config.get("adx_weak", 20.0)
        self.ATR_HIGH_PERCENTILE = self.config.get("atr_high", 80.0)
        self.ATR_LOW_PERCENTILE = self.config.get("atr_low", 20.0)
        self.MA_PERIOD = self.config.get("ma_period", 50)
        self.ATR_LOOKBACK = self.config.get("atr_lookback", 100)

        # 每个标的维护一个历史数据窗口
        self._data_buffers: Dict[str, deque] = {}

        logger.info(
            f"市场状态检测器已初始化 | "
            f"ADX 强={self.ADX_STRONG_THRESHOLD}, ADX 弱={self.ADX_WEAK_THRESHOLD} | "
            f"ATR 高={self.ATR_HIGH_PERCENTILE}%, ATR 低={self.ATR_LOW_PERCENTILE}%"
        )

    def update_data(self, code: str, kline: dict):
        """
        更新 K 线数据
        
        Args:
            code: 标的代码
            kline: K 线数据 dict (open, high, low, close, volume)
        """
        if code not in self._data_buffers:
            self._data_buffers[code] = deque(maxlen=self.ATR_LOOKBACK + 50)

        self._data_buffers[code].append(kline)

    def _get_data_arrays(self, code: str) -> Tuple[List, List, List, List]:
        """获取数据数组"""
        buffer = self._data_buffers.get(code, deque())
        if len(buffer) < self.MA_PERIOD + 10:
            return [], [], [], []

        highs = [k.get("high", 0) for k in buffer]
        lows = [k.get("low", 0) for k in buffer]
        closes = [k.get("close", 0) for k in buffer]
        volumes = [k.get("volume", 0) for k in buffer]

        return highs, lows, closes, volumes

    def _calculate_atr_percentile(self, highs: list, lows: list, closes: list) -> float:
        """计算当前 ATR 在历史中的百分位"""
        if len(closes) < self.ATR_LOOKBACK + 14:
            return 50.0  # 数据不足返回中性值

        atr_values = []
        for i in range(14, len(closes)):
            a = atr(highs[:i+1], lows[:i+1], closes[:i+1], 14)
            if a is not None:
                atr_values.append(a)

        if len(atr_values) < 20:
            return 50.0

        current_atr = atr_values[-1]
        percentile = sum(1 for x in atr_values if x < current_atr) / len(atr_values) * 100
        return percentile

    def _calculate_trend_direction(self, closes: list, adx_value: float) -> TrendDirection:
        """判断趋势方向"""
        if len(closes) < self.MA_PERIOD:
            return TrendDirection.NEUTRAL

        ma_value = sma(closes, self.MA_PERIOD)
        if ma_value is None or ma_value == 0:
            return TrendDirection.NEUTRAL

        current_price = closes[-1]
        price_vs_ma = (current_price - ma_value) / ma_value

        # ADX 极强（>50）时，放宽 MA 偏离阈值，避免高波动期间误判为 neutral
        # ADX=90+ 代表极强趋势，价格偏离哪怕只有 0.5% 也应被识别为趋势
        if adx_value >= 50:
            ma_offset = 0.005  # ADX 极强：偏离 0.5% 即为趋势
        elif adx_value >= self.ADX_STRONG_THRESHOLD:
            ma_offset = 0.02   # ADX 强：偏离 2% 为趋势
        else:
            ma_offset = 0.0    # ADX 弱：任何方向均可

        # 同时计算近期价格动量（最近5根K线趋势）
        recent_slope = 0.0
        if len(closes) >= 5:
            recent_slope = (closes[-1] - closes[-5]) / closes[-5] if closes[-5] else 0

        # 根据 ADX 和价格相对 MA 位置判断
        if adx_value >= self.ADX_STRONG_THRESHOLD:
            if price_vs_ma > ma_offset or (adx_value >= 50 and recent_slope > 0):
                return TrendDirection.STRONG_UP
            elif price_vs_ma < -ma_offset or (adx_value >= 50 and recent_slope < 0):
                return TrendDirection.STRONG_DOWN
        elif adx_value >= self.ADX_WEAK_THRESHOLD:
            if price_vs_ma > 0:
                return TrendDirection.WEAK_UP
            elif price_vs_ma < 0:
                return TrendDirection.WEAK_DOWN

        return TrendDirection.NEUTRAL

    def _calculate_volatility_state(self, atr_percentile: float) -> VolatilityState:
        """判断波动率状态"""
        if atr_percentile >= self.ATR_HIGH_PERCENTILE:
            return VolatilityState.HIGH
        elif atr_percentile <= self.ATR_LOW_PERCENTILE:
            return VolatilityState.LOW
        return VolatilityState.NORMAL

    def _calculate_bb_width(self, closes: list) -> float:
        """计算布林带宽度"""
        if len(closes) < 20:
            return 0

        bb = bollinger_bands(closes, 20, 2.0)
        if bb is None:
            return 0

        upper, middle, lower = bb
        if middle == 0:
            return 0

        return (upper - lower) / middle

    def detect(self, code: str) -> Optional[MarketRegime]:
        """
        检测当前市场状态
        
        Args:
            code: 标的代码
            
        Returns:
            MarketRegime 对象，如果数据不足返回 None
        """
        import time

        highs, lows, closes, volumes = self._get_data_arrays(code)

        if len(closes) < self.ATR_LOOKBACK:
            logger.debug(f"{code} 数据不足，无法判断市场状态")
            return None

        # 计算各指标
        adx_value = adx(highs, lows, closes, 14) or 0
        atr_value = atr(highs, lows, closes, 14) or 0
        atr_percentile = self._calculate_atr_percentile(highs, lows, closes)
        trend_direction = self._calculate_trend_direction(closes, adx_value)
        volatility_state = self._calculate_volatility_state(atr_percentile)
        bb_width = self._calculate_bb_width(closes)

        # 计算价格相对 MA 位置
        ma_value = sma(closes, self.MA_PERIOD)
        price_vs_ma = (closes[-1] - ma_value) / ma_value if ma_value else 0

        # 判断市场体制类型
        # 修复：ADX 极强（>50）时，趋势判断优先级高于波动率判断
        # 避免 ADX=96 + neutral 组合被误标为 high_volatility/range_bound
        if adx_value >= 50 and trend_direction != TrendDirection.NEUTRAL:
            # 极强趋势，忽略波动率状态，直接输出趋势类型
            if trend_direction == TrendDirection.STRONG_UP:
                regime_type = MarketRegimeType.STRONG_TREND_UP
            elif trend_direction == TrendDirection.WEAK_UP:
                regime_type = MarketRegimeType.WEAK_TREND_UP
            elif trend_direction == TrendDirection.STRONG_DOWN:
                regime_type = MarketRegimeType.STRONG_TREND_DOWN
            else:
                regime_type = MarketRegimeType.WEAK_TREND_DOWN
        elif volatility_state == VolatilityState.HIGH:
            regime_type = MarketRegimeType.HIGH_VOLATILITY
        elif volatility_state == VolatilityState.LOW:
            regime_type = MarketRegimeType.LOW_VOLATILITY
        elif trend_direction == TrendDirection.STRONG_UP:
            regime_type = MarketRegimeType.STRONG_TREND_UP
        elif trend_direction == TrendDirection.WEAK_UP:
            regime_type = MarketRegimeType.WEAK_TREND_UP
        elif trend_direction == TrendDirection.STRONG_DOWN:
            regime_type = MarketRegimeType.STRONG_TREND_DOWN
        elif trend_direction == TrendDirection.WEAK_DOWN:
            regime_type = MarketRegimeType.WEAK_TREND_DOWN
        else:
            regime_type = MarketRegimeType.RANGE_BOUND

        # 计算置信度
        confidence = self._calculate_confidence(adx_value, atr_percentile, trend_direction)

        regime = MarketRegime(
            code=code,
            regime_type=regime_type,
            trend_direction=trend_direction,
            volatility_state=volatility_state,
            adx_value=adx_value,
            atr_value=atr_value,
            atr_percentile=atr_percentile,
            price_vs_ma=price_vs_ma,
            bb_width=bb_width,
            confidence=confidence,
            timestamp=time.time(),
        )

        logger.info(
            f"[{code}] 市场状态：{regime_type.value} | "
            f"ADX={adx_value:.1f} | ATR%={atr_percentile:.1f} | "
            f"趋势={trend_direction.value} | 置信度={confidence:.2f}"
        )

        return regime

    def _calculate_confidence(self, adx: float, atr_pct: float, trend: TrendDirection) -> float:
        """
        计算判断置信度 (0-1)
        
        考虑因素:
        - ADX 值越高，趋势判断越可靠
        - ATR 百分位极端值，波动率判断越可靠
        - 多个指标一致时置信度更高
        """
        confidence = 0.5

        # ADX 贡献
        if adx >= self.ADX_STRONG_THRESHOLD:
            confidence += 0.2
        elif adx >= self.ADX_WEAK_THRESHOLD:
            confidence += 0.1

        # ATR 百分位贡献
        if atr_pct >= self.ATR_HIGH_PERCENTILE or atr_pct <= self.ATR_LOW_PERCENTILE:
            confidence += 0.15

        # 趋势强度贡献
        if trend in (TrendDirection.STRONG_UP, TrendDirection.STRONG_DOWN):
            confidence += 0.15
        elif trend in (TrendDirection.WEAK_UP, TrendDirection.WEAK_DOWN):
            confidence += 0.05

        return min(confidence, 1.0)

    def get_recommended_strategies(self, regime: MarketRegime) -> Dict[str, float]:
        """
        根据市场状态推荐策略及权重
        
        Returns:
            {策略名：权重 (0-1)}
        """
        recommendations = {}

        # 基础权重
        base_strategies = {
            "supertrend": 0.5,
            "macd": 0.5,
            "rsi": 0.5,
            "bollinger": 0.5,
            "confluence": 0.7,
        }

        if regime.regime_type in (MarketRegimeType.STRONG_TREND_UP, MarketRegimeType.STRONG_TREND_DOWN):
            # 强趋势：提升趋势策略权重，降低震荡策略
            recommendations = {
                "supertrend": 1.0,
                "macd": 0.9,
                "rsi": 0.3,      # 降低 RSI 反转信号
                "bollinger": 0.4,
                "confluence": 1.0,
            }
        elif regime.regime_type == MarketRegimeType.RANGE_BOUND:
            # 震荡市：提升震荡策略权重
            recommendations = {
                "supertrend": 0.3,  # 降低趋势策略
                "macd": 0.4,
                "rsi": 0.9,         # 提升 RSI
                "bollinger": 0.9,   # 提升布林带
                "confluence": 0.8,
            }
        elif regime.regime_type == MarketRegimeType.HIGH_VOLATILITY:
            # 高波动：降低整体仓位
            recommendations = {
                "supertrend": 0.6,
                "macd": 0.6,
                "rsi": 0.5,
                "bollinger": 0.5,
                "confluence": 0.9,  # 多因子确认更重要
            }
        elif regime.regime_type == MarketRegimeType.LOW_VOLATILITY:
            # 低波动：可能是突破前兆
            recommendations = {
                "supertrend": 0.5,
                "macd": 0.5,
                "rsi": 0.5,
                "bollinger": 0.7,   # 布林带收窄可能突破
                "confluence": 0.8,
            }
        else:
            recommendations = base_strategies

        return recommendations


# 全局单例 (延迟加载配置，避免循环导入)
_market_regime_detector_instance: Optional['MarketRegimeDetector'] = None

def get_market_regime_detector() -> 'MarketRegimeDetector':
    """获取市场状态检测器单例，首次调用时从 YAML 加载配置"""
    global _market_regime_detector_instance
    if _market_regime_detector_instance is None:
        try:
            from config.config_center import get_config_center
            center = get_config_center()
            cfg = center.get("market_regime", {}) if center else {}
        except Exception:
            cfg = {}
        _market_regime_detector_instance = MarketRegimeDetector(cfg)
    return _market_regime_detector_instance

# 向后兼容代理
class _LazyDetector:
    """延迟代理，兼容旧代码 market_regime_detector.detect(code) 调用"""
    def __getattr__(self, name):
        return getattr(get_market_regime_detector(), name)

market_regime_detector = _LazyDetector()
