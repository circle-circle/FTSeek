"""
特征提取器 (Feature Extractor)

从行情 K 线数据中提取用于场景识别的 6 大维度特征:
1. 趋势特征 - ADX, MA 斜率，均线排列，高低点结构
2. 波动特征 - ATR, 布林带宽度，实现波动率
3. 量能特征 - 成交量比率，成交量趋势
4. 结构特征 - 箱体区间，突破水平
5. 事件特征 - 跳空比例，新闻情绪
6. 位置特征 - 价格相对 VWAP/MA 位置，RSI
"""
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from datetime import datetime

from app.strategy.indicators import (
    adx, atr, sma, ema, bollinger_bands, bollinger_pctb,
    supertrend, rsi, macd
)

logger = logging.getLogger("ftseek.scenario.features")
logger.propagate = False


@dataclass
class TrendFeatures:
    """趋势特征"""
    adx: float = 0.0              # ADX 值 (趋势强度)
    ma_slope: float = 0.0         # 均线斜率
    ma_alignment: str = "neutral" # bullish/bearish/neutral (均线排列)
    structure: str = "unknown"    # HH_HL/LH_LL/unknown (高低点结构)
    pullback_quality: float = 0.5 # 回撤质量 (0-1)


@dataclass
class VolatilityFeatures:
    """波动特征"""
    atr: float = 0.0              # ATR 值
    atr_ratio: float = 1.0        # ATR 相对均线比率
    bb_width: float = 0.0         # 布林带宽度
    realized_vol: float = 0.0     # 实现波动率


@dataclass
class VolumeFeatures:
    """量能特征"""
    volume_ratio: float = 1.0     # 当前成交量/均量比
    volume_slope: float = 0.0     # 成交量趋势 (斜率)


@dataclass
class StructureFeatures:
    """结构特征"""
    box_range: float = 0.0        # 箱体区间高度百分比
    breakout_level: float = 0.0   # 突破水平 (0-1, 1=完全突破)
    box_top: float = 0.0          # 箱体顶部
    box_bottom: float = 0.0       # 箱体底部


@dataclass
class EventFeatures:
    """事件特征"""
    gap_pct: float = 0.0          # 跳空百分比
    news_score: float = 0.0       # 新闻情绪得分
    has_announcement: bool = False # 是否有公告


@dataclass
class PositionFeatures:
    """位置特征"""
    price_vs_ma: float = 0.0      # 价格相对 MA 位置 (%)
    price_vs_vwap: float = 0.0    # 价格相对 VWAP 位置 (%)
    rsi: float = 50.0             # RSI 值
    pctb: float = 0.5             # 布林带位置 (0-1)


@dataclass
class Features:
    """
    特征集合
    """
    trend: TrendFeatures = field(default_factory=TrendFeatures)
    volatility: VolatilityFeatures = field(default_factory=VolatilityFeatures)
    volume: VolumeFeatures = field(default_factory=VolumeFeatures)
    structure: StructureFeatures = field(default_factory=StructureFeatures)
    event: EventFeatures = field(default_factory=EventFeatures)
    position: PositionFeatures = field(default_factory=PositionFeatures)

    # 元数据
    code: str = ""
    timestamp: str = ""
    kline_count: int = 0


class FeatureExtractor:
    """
    特征提取器
    """

    def __init__(self, config: Dict = None):
        """
        初始化特征提取器

        Args:
            config: 配置字典
        """
        self.config = config or {}

        # 指标参数
        self.adx_period = self.config.get('adx_period', 14)
        self.atr_period = self.config.get('atr_period', 14)
        self.ma_period = self.config.get('ma_period', 20)
        self.bb_period = self.config.get('bb_period', 20)
        self.bb_std = self.config.get('bb_std', 2.0)
        self.rsi_period = self.config.get('rsi_period', 14)
        self.volume_period = self.config.get('volume_period', 20)

        logger.info("特征提取器已初始化")

    def extract(self, code: str, klines: List[Dict], news_data: Dict = None) -> Features:
        """
        从 K 线数据中提取特征

        Args:
            code: 股票代码
            klines: K 线数据列表，每项包含 {open, high, low, close, volume, timestamp}
            news_data: 新闻数据 (可选)

        Returns:
            Features: 特征集合
        """
        if len(klines) < self.ma_period + 10:
            logger.debug(f"{code} K 线数据不足，返回默认特征")
            return self._default_features(code)

        # 提取数据数组
        highs = [k['high'] for k in klines]
        lows = [k['low'] for k in klines]
        closes = [k['close'] for k in klines]
        opens = [k['open'] for k in klines]
        volumes = [k['volume'] for k in klines]

        # 计算各维度特征
        features = Features(
            code=code,
            timestamp=datetime.now().isoformat(),
            kline_count=len(klines),
        )

        # 1. 趋势特征
        features.trend = self._extract_trend_features(highs, lows, closes)

        # 2. 波动特征
        features.volatility = self._extract_volatility_features(highs, lows, closes)

        # 3. 量能特征
        features.volume = self._extract_volume_features(volumes)

        # 4. 结构特征
        features.structure = self._extract_structure_features(highs, lows, closes)

        # 5. 事件特征
        features.event = self._extract_event_features(opens, closes, news_data)

        # 6. 位置特征
        features.position = self._extract_position_features(highs, lows, closes, volumes)

        return features

    def _extract_trend_features(self, highs: List[float], lows: List[float],
                                 closes: List[float]) -> TrendFeatures:
        """提取趋势特征"""
        # ADX
        adx_value = adx(highs, lows, closes, self.adx_period) or 0.0

        # MA 斜率
        ma_current = sma(closes, self.ma_period) or closes[-1]
        ma_prev = sma(closes[:-1], self.ma_period) if len(closes) > self.ma_period else ma_current
        ma_slope = (ma_current - ma_prev) / ma_prev if ma_prev else 0.0

        # 均线排列
        ma_fast = ema(closes, 10) if len(closes) >= 10 else closes[-1]
        ma_mid = sma(closes, 20) if len(closes) >= 20 else closes[-1]
        ma_slow = sma(closes, 50) if len(closes) >= 50 else closes[-1]

        if ma_fast > ma_mid > ma_slow:
            ma_alignment = "bullish"
        elif ma_fast < ma_mid < ma_slow:
            ma_alignment = "bearish"
        else:
            ma_alignment = "neutral"

        # 高低点结构
        structure = self._identify_structure(highs, lows)

        # 回撤质量 (简单实现：回撤深度/ADX)
        pullback_quality = 0.5  # 待完善

        return TrendFeatures(
            adx=adx_value,
            ma_slope=ma_slope,
            ma_alignment=ma_alignment,
            structure=structure,
            pullback_quality=pullback_quality,
        )

    def _extract_volatility_features(self, highs: List[float], lows: List[float],
                                      closes: List[float]) -> VolatilityFeatures:
        """提取波动特征"""
        # ATR
        atr_value = atr(highs, lows, closes, self.atr_period) or 0.0

        # ATR 比率 (当前 ATR / 平均 ATR)
        atr_values = []
        for i in range(self.atr_period, len(closes)):
            a = atr(highs[:i], lows[:i], closes[:i], self.atr_period)
            if a is not None:
                atr_values.append(a)
        atr_ma = sum(atr_values) / len(atr_values) if atr_values else atr_value
        atr_ratio = atr_value / atr_ma if atr_ma else 1.0

        # 布林带宽度
        bb = bollinger_bands(closes, self.bb_period, self.bb_std)
        if bb:
            upper, middle, lower = bb
            bb_width = (upper - lower) / middle if middle else 0.0
        else:
            bb_width = 0.0

        # 实现波动率 (简化：收益率标准差)
        returns = [(closes[i] - closes[i-1]) / closes[i-1]
                   for i in range(1, len(closes))]
        realized_vol = (sum(r**2 for r in returns) / len(returns)) ** 0.5 if returns else 0.0

        return VolatilityFeatures(
            atr=atr_value,
            atr_ratio=atr_ratio,
            bb_width=bb_width,
            realized_vol=realized_vol,
        )

    def _extract_volume_features(self, volumes: List[float]) -> VolumeFeatures:
        """提取量能特征"""
        if len(volumes) < self.volume_period:
            return VolumeFeatures()

        # 成交量比率
        avg_volume = sum(volumes[-self.volume_period:]) / self.volume_period
        volume_ratio = volumes[-1] / avg_volume if avg_volume else 1.0

        # 成交量趋势 (简单斜率)
        recent_vol = volumes[-5:]
        if len(recent_vol) >= 2:
            volume_slope = (recent_vol[-1] - recent_vol[0]) / recent_vol[0] if recent_vol[0] else 0.0
        else:
            volume_slope = 0.0

        return VolumeFeatures(
            volume_ratio=volume_ratio,
            volume_slope=volume_slope,
        )

    def _extract_structure_features(self, highs: List[float], lows: List[float],
                                     closes: List[float]) -> StructureFeatures:
        """提取结构特征"""
        # 识别箱体 (简化：最近 N 根 K 线的最高/最低)
        lookback = 20
        if len(highs) < lookback:
            return StructureFeatures()

        recent_highs = highs[-lookback:]
        recent_lows = lows[-lookback:]

        box_top = max(recent_highs)
        box_bottom = min(recent_lows)
        box_range = (box_top - box_bottom) / box_bottom if box_bottom else 0.0

        # 突破水平
        current_price = closes[-1]
        if box_top > box_bottom:
            breakout_level = (current_price - box_top) / (box_top - box_bottom)
        else:
            breakout_level = 0.0

        return StructureFeatures(
            box_range=box_range,
            breakout_level=breakout_level,
            box_top=box_top,
            box_bottom=box_bottom,
        )

    def _extract_event_features(self, opens: List[float], closes: List[float],
                                 news_data: Dict = None) -> EventFeatures:
        """提取事件特征"""
        # 跳空百分比 (当前开盘 vs 前一收盘)
        if len(opens) >= 2:
            gap_pct = (opens[-1] - closes[-2]) / closes[-2] * 100 if closes[-2] else 0.0
        else:
            gap_pct = 0.0

        # 新闻情绪
        news_score = news_data.get('sentiment_score', 0.0) if news_data else 0.0
        has_announcement = news_data.get('has_announcement', False) if news_data else False

        return EventFeatures(
            gap_pct=gap_pct,
            news_score=news_score,
            has_announcement=has_announcement,
        )

    def _extract_position_features(self, highs: List[float], lows: List[float],
                                    closes: List[float], volumes: List[float]) -> PositionFeatures:
        """提取位置特征"""
        # 价格相对 MA 位置
        ma_value = sma(closes, self.ma_period) if len(closes) >= self.ma_period else closes[-1]
        price_vs_ma = (closes[-1] - ma_value) / ma_value if ma_value else 0.0

        # 价格相对 VWAP 位置 (简化：用 MA 代替 VWAP)
        price_vs_vwap = price_vs_ma  # 简化实现

        # RSI
        rsi_value = rsi(closes, self.rsi_period) if len(closes) >= self.rsi_period else 50.0

        # 布林带位置
        bb = bollinger_bands(closes, self.bb_period, self.bb_std)
        if bb:
            upper, middle, lower = bb
            pctb_value = bollinger_pctb(closes[-1], upper, lower)
            pctb = pctb_value if pctb_value is not None else 0.5
        else:
            pctb = 0.5

        return PositionFeatures(
            price_vs_ma=price_vs_ma,
            price_vs_vwap=price_vs_vwap,
            rsi=rsi_value,
            pctb=pctb,
        )

    def _identify_structure(self, highs: List[float], lows: List[float]) -> str:
        """
        识别高低点结构

        Returns:
            HH_HL: 更高高点 + 更高低点 (上涨结构)
            LH_LL: 更低高点 + 更低低点 (下跌结构)
            unknown: 无明确结构
        """
        if len(highs) < 10 or len(lows) < 10:
            return "unknown"

        # 找最近 3 个高点和 3 个低点
        recent_highs = highs[-10:]
        recent_lows = lows[-10:]

        # 简化：比较首尾
        if highs[-1] > highs[-5] > highs[-10] and lows[-1] > lows[-5] > lows[-10]:
            return "HH_HL"  # 上涨结构
        elif highs[-1] < highs[-5] < highs[-10] and lows[-1] < lows[-5] < lows[-10]:
            return "LH_LL"  # 下跌结构

        return "unknown"

    def _default_features(self, code: str) -> Features:
        """返回默认特征 (数据不足时)"""
        return Features(
            code=code,
            timestamp=datetime.now().isoformat(),
            kline_count=0,
        )


# 全局单例
_feature_extractor: Optional[FeatureExtractor] = None


def get_feature_extractor(config: Dict = None) -> FeatureExtractor:
    """获取特征提取器单例"""
    global _feature_extractor
    if _feature_extractor is None:
        _feature_extractor = FeatureExtractor(config)
    return _feature_extractor
