"""
独立技术因子模块 (2026-04-06 新增)

将 RSI、MACD、Bollinger、SuperTrend 等技术指标封装为独立因子类，
支持：
- 单独启用/禁用
- 独立配置参数
- 动态权重调整
- 因子组合复用

使用示例:
    from app.factors.technical import RSIFactor, MACDFactor

    rsi = RSIFactor(period=14, overbought=70, oversold=30)
    signal = rsi.evaluate(closes)  # 返回 FactorSignal

    macd = MACDFactor.from_config(config_dict)
    signal = macd.evaluate(closes)
"""
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Dict, Any, List
from app.strategy.indicators import (
    rsi, macd, bollinger_bands, bollinger_pctb,
    supertrend, sma, ema, atr, adx
)

logger = logging.getLogger("ftseek.factors")


class FactorDirection(str, Enum):
    """因子信号方向"""
    BULLISH = "bullish"       # 看多
    BEARISH = "bearish"       # 看空
    NEUTRAL = "neutral"       # 中性


@dataclass
class FactorSignal:
    """
    因子信号

    Attributes:
        direction: 信号方向
        strength: 信号强度 (0-1)
        value: 因子当前值
        reason: 信号原因描述
        weight: 因子权重 (默认 1.0)
    """
    direction: FactorDirection
    strength: float
    value: float
    reason: str
    weight: float = 1.0

    @property
    def weighted_strength(self) -> float:
        """加权信号强度"""
        return self.strength * self.weight

    def to_dict(self) -> Dict:
        """转换为字典"""
        return {
            'direction': self.direction.value,
            'strength': self.strength,
            'weighted_strength': self.weighted_strength,
            'value': self.value,
            'reason': self.reason,
            'weight': self.weight,
        }


class BaseFactor(ABC):
    """
    因子基类

    所有技术因子必须继承此类，实现:
    - name: 因子名称
    - evaluate: 评估方法
    """
    name: str = "base_factor"

    def __init__(self, config: Dict[str, Any] = None):
        """
        初始化因子

        Args:
            config: 因子配置字典
        """
        self.config = config or {}
        self.enabled = self.config.get('enabled', True)

    @abstractmethod
    def evaluate(self, **kwargs) -> Optional[FactorSignal]:
        """
        评估因子，产生信号

        Returns:
            FactorSignal 或 None
        """
        pass

    def get_params(self) -> Dict:
        """获取因子参数"""
        return {'name': self.name, 'enabled': self.enabled}

    def update_params(self, params: Dict):
        """动态更新参数"""
        for key, value in params.items():
            if hasattr(self, key):
                setattr(self, key, value)
            elif key in self.config:
                self.config[key] = value


# ============================================================
#  RSI 因子
# ============================================================
class RSIFactor(BaseFactor):
    """
    RSI 超买超卖因子

    逻辑:
    - RSI 从超卖区 (<30) 反转向上 -> 看多
    - RSI 从超买区 (>70) 反转向下 -> 看空
    - RSI 在超卖区 (<30) -> 看多
    - RSI 在超买区 (>70) -> 看空
    """
    name = "rsi"

    def __init__(self, config: Dict[str, Any] = None):
        super().__init__(config)
        self.period = self.config.get('period', 14)
        self.overbought = self.config.get('overbought', 70)
        self.oversold = self.config.get('oversold', 30)
        self._prev_rsi: Optional[float] = None

    def evaluate(self, closes: List[float], **kwargs) -> Optional[FactorSignal]:
        if not self.enabled or len(closes) < self.period + 1:
            return None

        rsi_val = rsi(closes, self.period)
        if rsi_val is None:
            return None

        direction = FactorDirection.NEUTRAL
        strength = 0.0
        reason = ""

        # 检测反转
        if self._prev_rsi is not None:
            if self._prev_rsi <= self.oversold and rsi_val > self.oversold:
                direction = FactorDirection.BULLISH
                strength = 0.8
                reason = f"RSI 超卖反转 ({self._prev_rsi:.1f}->{rsi_val:.1f})"
            elif self._prev_rsi >= self.overbought and rsi_val < self.overbought:
                direction = FactorDirection.BEARISH
                strength = 0.8
                reason = f"RSI 超买回落 ({self._prev_rsi:.1f}->{rsi_val:.1f})"

        # 检测极端区域
        if direction == FactorDirection.NEUTRAL:
            if rsi_val < self.oversold:
                direction = FactorDirection.BULLISH
                strength = 0.5
                reason = f"RSI 超卖区 ({rsi_val:.1f})"
            elif rsi_val > self.overbought:
                direction = FactorDirection.BEARISH
                strength = 0.5
                reason = f"RSI 超买区 ({rsi_val:.1f})"

        self._prev_rsi = rsi_val

        if direction == FactorDirection.NEUTRAL:
            return None

        return FactorSignal(
            direction=direction,
            strength=strength,
            value=rsi_val,
            reason=reason,
            weight=self.config.get('weight', 1.0)
        )


# ============================================================
#  MACD 因子
# ============================================================
class MACDFactor(BaseFactor):
    """
    MACD 趋势跟踪因子

    逻辑:
    - MACD 柱状图 > 0 -> 看多
    - MACD 柱状图 < 0 -> 看空
    """
    name = "macd"

    def __init__(self, config: Dict[str, Any] = None):
        super().__init__(config)
        self.fast_period = self.config.get('fast_period', 12)
        self.slow_period = self.config.get('slow_period', 26)
        self.signal_period = self.config.get('signal_period', 9)
        self._prev_histogram: Optional[float] = None

    def evaluate(self, closes: List[float], **kwargs) -> Optional[FactorSignal]:
        if not self.enabled or len(closes) < self.slow_period + self.signal_period:
            return None

        result = macd(closes, self.fast_period, self.slow_period, self.signal_period)
        if result is None:
            return None

        macd_line, signal_line, histogram = result

        direction = FactorDirection.NEUTRAL
        strength = 0.0
        reason = ""

        # 基于柱状图方向
        if histogram > 0:
            direction = FactorDirection.BULLISH
            strength = min(1.0, abs(histogram) * 100)  # 归一化
            reason = f"MACD 金叉 (hist={histogram:.4f})"
        elif histogram < 0:
            direction = FactorDirection.BEARISH
            strength = min(1.0, abs(histogram) * 100)
            reason = f"MACD 死叉 (hist={histogram:.4f})"

        # 检测交叉
        if self._prev_histogram is not None:
            if self._prev_histogram <= 0 and histogram > 0:
                direction = FactorDirection.BULLISH
                strength = 0.9
                reason = f"MACD 柱状图由负转正 (金叉确认)"
            elif self._prev_histogram >= 0 and histogram < 0:
                direction = FactorDirection.BEARISH
                strength = 0.9
                reason = f"MACD 柱状图由正转负 (死叉确认)"

        self._prev_histogram = histogram

        if direction == FactorDirection.NEUTRAL:
            return None

        return FactorSignal(
            direction=direction,
            strength=strength,
            value=histogram,
            reason=reason,
            weight=self.config.get('weight', 1.0)
        )


# ============================================================
#  Bollinger Bands 因子
# ============================================================
class BollingerFactor(BaseFactor):
    """
    布林带均值回归因子

    逻辑:
    - %B <= 0.1 (接近下轨) -> 看多
    - %B >= 0.9 (接近上轨) -> 看空
    """
    name = "bollinger"

    def __init__(self, config: Dict[str, Any] = None):
        super().__init__(config)
        self.period = self.config.get('period', 20)
        self.std_dev = self.config.get('std_dev', 2.0)
        self.mode = self.config.get('mode', 'reversion')  # reversion | breakout

    def evaluate(self, closes: List[float], **kwargs) -> Optional[FactorSignal]:
        if not self.enabled or len(closes) < self.period:
            return None

        bb = bollinger_bands(closes, self.period, self.std_dev)
        if bb is None:
            return None

        upper, middle, lower = bb
        close = closes[-1]
        pctb = bollinger_pctb(close, upper, lower)

        direction = FactorDirection.NEUTRAL
        strength = 0.0
        reason = ""

        if self.mode == 'reversion':
            # 均值回归模式
            if pctb <= 0.1:
                direction = FactorDirection.BULLISH
                strength = 1.0 - pctb * 10
                reason = f"布林带下轨 (%B={pctb:.2f})"
            elif pctb >= 0.9:
                direction = FactorDirection.BEARISH
                strength = pctb * 10 - 9.0
                reason = f"布林带上轨 (%B={pctb:.2f})"
        else:
            # 突破模式
            if pctb >= 1.0:
                direction = FactorDirection.BULLISH
                strength = 0.7
                reason = f"布林带向上突破 (%B={pctb:.2f})"
            elif pctb <= 0.0:
                direction = FactorDirection.BEARISH
                strength = 0.7
                reason = f"布林带向下突破 (%B={pctb:.2f})"

        if direction == FactorDirection.NEUTRAL:
            return None

        return FactorSignal(
            direction=direction,
            strength=strength,
            value=pctb,
            reason=reason,
            weight=self.config.get('weight', 1.0)
        )


# ============================================================
#  SuperTrend 因子
# ============================================================
class SuperTrendFactor(BaseFactor):
    """
    SuperTrend 趋势跟踪因子

    逻辑:
    - SuperTrend 方向向上 -> 看多
    - SuperTrend 方向向下 -> 看空
    """
    name = "supertrend"

    def __init__(self, config: Dict[str, Any] = None):
        super().__init__(config)
        self.atr_period = self.config.get('atr_period', 10)
        self.multiplier = self.config.get('multiplier', 3.0)

    def evaluate(self, highs: List[float], lows: List[float], closes: List[float],
                 **kwargs) -> Optional[FactorSignal]:
        if not self.enabled or len(closes) < self.atr_period + 5:
            return None

        result = supertrend(highs, lows, closes, self.atr_period, self.multiplier)
        if result is None:
            return None

        st_val, direction_str = result

        direction = FactorDirection.BULLISH if direction_str == "up" else FactorDirection.BEARISH
        strength = 0.7  # SuperTrend 信号强度固定
        reason = f"SuperTrend {direction_str}"

        return FactorSignal(
            direction=direction,
            strength=strength,
            value=st_val,
            reason=reason,
            weight=self.config.get('weight', 1.0)
        )


# ============================================================
#  MA Cross 因子
# ============================================================
class MACrossFactor(BaseFactor):
    """
    均线交叉因子

    逻辑:
    - 快线 > 慢线 -> 看多
    - 快线 < 慢线 -> 看空
    """
    name = "ma_cross"

    def __init__(self, config: Dict[str, Any] = None):
        super().__init__(config)
        self.ma_fast_period = self.config.get('ma_fast_period', 10)
        self.ma_slow_period = self.config.get('ma_slow_period', 30)
        self.ma_type = self.config.get('ma_type', 'sma')  # sma | ema

    def evaluate(self, closes: List[float], **kwargs) -> Optional[FactorSignal]:
        if not self.enabled or len(closes) < self.ma_slow_period:
            return None

        if self.ma_type == 'ema':
            fast_ma = ema(closes, self.ma_fast_period)
            slow_ma = ema(closes, self.ma_slow_period)
        else:
            fast_ma = sma(closes, self.ma_fast_period)
            slow_ma = sma(closes, self.ma_slow_period)

        if fast_ma is None or slow_ma is None:
            return None

        direction = FactorDirection.BULLISH if fast_ma > slow_ma else FactorDirection.BEARISH
        strength = abs(fast_ma - slow_ma) / slow_ma * 10  # 归一化
        strength = min(1.0, strength)
        reason = f"MA 交叉 (快={fast_ma:.2f}, 慢={slow_ma:.2f})"

        return FactorSignal(
            direction=direction,
            strength=strength,
            value=fast_ma,
            reason=reason,
            weight=self.config.get('weight', 1.0)
        )


# ============================================================
#  Volume 因子
# ============================================================
class VolumeFactor(BaseFactor):
    """
    成交量因子

    逻辑:
    - 放量上涨 -> 看多
    - 放量下跌 -> 看空
    """
    name = "volume"

    def __init__(self, config: Dict[str, Any] = None):
        super().__init__(config)
        self.lookback_periods = self.config.get('lookback_periods', 20)
        self.volume_ratio_threshold = self.config.get('volume_ratio_threshold', 1.5)

    def evaluate(self, volumes: List[float], closes: List[float], **kwargs) -> Optional[FactorSignal]:
        if not self.enabled or len(volumes) < self.lookback_periods + 1:
            return None

        avg_vol = sum(volumes[-self.lookback_periods - 1:-1]) / self.lookback_periods
        if avg_vol <= 0:
            return None

        current_vol = volumes[-1]
        vol_ratio = current_vol / avg_vol

        if vol_ratio < self.volume_ratio_threshold:
            return None

        # 放量确认方向
        if len(closes) > 1 and closes[-1] > closes[-2]:
            direction = FactorDirection.BULLISH
            reason = f"放量上涨 (vol={vol_ratio:.2f}x)"
        else:
            direction = FactorDirection.BEARISH
            reason = f"放量下跌 (vol={vol_ratio:.2f}x)"

        strength = min(1.0, (vol_ratio - self.volume_ratio_threshold) / 2)

        return FactorSignal(
            direction=direction,
            strength=strength,
            value=vol_ratio,
            reason=reason,
            weight=self.config.get('weight', 1.0)
        )


# ============================================================
#  ADX 趋势强度因子 (过滤器)
# ============================================================
class ADXFilterFactor(BaseFactor):
    """
    ADX 趋势强度过滤器

    不产生独立信号，用于过滤其他因子信号
    ADX > 25 表示强趋势，信号可信度更高
    """
    name = "adx_filter"

    def __init__(self, config: Dict[str, Any] = None):
        super().__init__(config)
        self.threshold = self.config.get('threshold', 25)

    def evaluate(self, highs: List[float], lows: List[float],
                 closes: List[float], **kwargs) -> Optional[FactorSignal]:
        if not self.enabled or len(closes) < self.threshold * 2 + 1:
            return None

        adx_val = adx(highs, lows, closes, int(self.threshold))
        if adx_val is None:
            return None

        # ADX 只判断趋势强度，不判断方向
        if adx_val >= self.threshold:
            return FactorSignal(
                direction=FactorDirection.BULLISH,  # 表示"有效"
                strength=min(1.0, adx_val / 50),
                value=adx_val,
                reason=f"ADX 强趋势 ({adx_val:.1f})",
                weight=1.0
            )
        else:
            return FactorSignal(
                direction=FactorDirection.NEUTRAL,
                strength=adx_val / self.threshold,
                value=adx_val,
                reason=f"ADX 弱趋势 ({adx_val:.1f})",
                weight=1.0
            )

    def is_strong_trend(self, highs, lows, closes) -> bool:
        """是否处于强趋势"""
        signal = self.evaluate(highs, lows, closes)
        return signal is not None and signal.direction != FactorDirection.NEUTRAL


# ============================================================
#  因子工厂
# ============================================================
FACTOR_REGISTRY = {
    'rsi': RSIFactor,
    'macd': MACDFactor,
    'bollinger': BollingerFactor,
    'supertrend': SuperTrendFactor,
    'ma_cross': MACrossFactor,
    'volume': VolumeFactor,
    'adx_filter': ADXFilterFactor,
}


def create_factor(name: str, config: Dict[str, Any] = None) -> BaseFactor:
    """
    创建因子实例

    Args:
        name: 因子名称
        config: 因子配置

    Returns:
        因子实例
    """
    cls = FACTOR_REGISTRY.get(name)
    if cls is None:
        raise ValueError(f"未知因子：{name}, 可用：{list(FACTOR_REGISTRY.keys())}")
    return cls(config)


def list_factors() -> List[str]:
    """列出所有可用因子"""
    return list(FACTOR_REGISTRY.keys())
