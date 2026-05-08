"""
因子投票策略 (Factor Vote Strategies)

将 confluence_v2 内部的 6 个技术因子拆分为独立策略，
每个因子向共识引擎投出自己的一票。

架构收益:
    - 共识引擎直接看到 6 票而非 1 张 confluence_v2 HYBRID 票
    - DIVERGENCE_SCORE_DECAY 自动生效 (RSI vs MACD 分歧时降权)
    - TREND_BOOST / RANGE_BOOST 自动生效 (趋势/震荡因子自动调权)
    - 信号来源完全透明, 告别黑盒

因子分类:
    MEAN_REVERSION: rsi_factor, bb_factor         → 震荡市 ×1.5 增益
    MOMENTUM:       macd_factor, st_factor,         → 趋势市 ×1.5 增益
                    ma_cross_factor, vol_factor

设计原则:
    1. 每个策略极简 (30-60 行), 只做单一因子计算
    2. 使用协调器传递的 shared_buffer (单一数据源)
    3. 不维护独立 K 线缓存
    4. 与 confluence_v2 并存 (HYBRID 融合策略作为确认层)
"""
import logging
from typing import Optional, Dict, List, Any

from .base import BaseStrategy, Signal, SignalAction
from .base import StrategyMetadata, FactorType, TimeHorizon, RiskLevel
from .indicators import (
    rsi as calc_rsi,
    macd as calc_macd,
    bollinger_bands,
    bollinger_pctb,
    supertrend as calc_supertrend,
    sma,
    volatility_factor,
    volatility_adaptive_multiplier,
)
from app.core.types import SignalSource

logger = logging.getLogger("ftseek.strategy.factor_votes")
logger.propagate = False

# ============================================================
#  辅助函数: 从共享缓冲提取 OHLCV
# ============================================================

def _extract_ohlcv(shared_buffer: list) -> tuple:
    """从共享 K 线缓冲中提取 OHLCV 数组 (单次遍历)"""
    closes, highs, lows, volumes = [], [], [], []
    for k in shared_buffer:
        c = k.get("close", 0)
        h = k.get("high", 0) or c
        l = k.get("low", 0) or c
        v = k.get("volume", 0)
        if c > 0:
            closes.append(c)
            highs.append(h)
            lows.append(l)
            volumes.append(v)
    return closes, highs, lows, volumes


# ============================================================
#  Factor 1: RSI
# ============================================================

class RSIFactorStrategy(BaseStrategy):
    """RSI 超买超卖因子 — MEAN_REVERSION"""

    name = "rsi_factor"
    signal_channels = ["on_kline"]

    metadata = StrategyMetadata(
        factor_type=FactorType.MEAN_REVERSION,
        horizon=TimeHorizon.SWING,
        regime_affinity=["ranging"],
        risk_level=RiskLevel.LOW,
        required_klines=20,
        description="RSI 超买超卖检测",
    )

    def __init__(self, cfg: dict = None):
        super().__init__(cfg or {})
        self.period = self.config.get("rsi_period", 14)
        self.overbought = self.config.get("rsi_overbought", 70)
        self.oversold = self.config.get("rsi_oversold", 30)
        self._prev_rsi: Dict[str, float] = {}

    def on_kline(self, code: str, kline: dict, kline_type: str,
                 shared_buffer: list = None) -> Optional[Signal]:
        if not self.enabled or kline_type != "K_1M" or not shared_buffer:
            return None
        closes, _, _, _ = _extract_ohlcv(shared_buffer)
        if len(closes) < self.period + 2:
            return None
        rsi_val = calc_rsi(closes, self.period)
        if rsi_val is None:
            return None
        prev = self._prev_rsi.get(code)
        self._prev_rsi[code] = rsi_val

        if prev is None:
            return None

        if prev <= self.oversold and rsi_val > self.oversold:
            return Signal(code=code, action=SignalAction.BUY, price=closes[-1],
                          source=SignalSource.TECHNICAL,
                          reason=f"RSI 超卖反转 ({prev:.1f}→{rsi_val:.1f})",
                          strategy=self.name)
        if prev >= self.overbought and rsi_val < self.overbought:
            return Signal(code=code, action=SignalAction.SELL, price=closes[-1],
                          source=SignalSource.TECHNICAL,
                          reason=f"RSI 超买回落 ({prev:.1f}→{rsi_val:.1f})",
                          strategy=self.name)
        return None


# ============================================================
#  Factor 2: MACD
# ============================================================

class MACDFactorStrategy(BaseStrategy):
    """MACD 动量因子 — MOMENTUM"""

    name = "macd_factor"
    signal_channels = ["on_kline"]

    metadata = StrategyMetadata(
        factor_type=FactorType.MOMENTUM,
        horizon=TimeHorizon.SWING,
        regime_affinity=["trending"],
        risk_level=RiskLevel.MODERATE,
        required_klines=35,
        description="MACD 柱状图方向 + 滞回",
    )

    def __init__(self, cfg: dict = None):
        super().__init__(cfg or {})
        self.fast = self.config.get("macd_fast", 12)
        self.slow = self.config.get("macd_slow", 26)
        self.signal = self.config.get("macd_signal", 9)
        self.hysteresis_pct = self.config.get("macd_hysteresis_pct", 0.005)
        self._prev_hist: Dict[str, float] = {}
        self._prev_dir: Dict[str, int] = {}

    def on_kline(self, code: str, kline: dict, kline_type: str,
                 shared_buffer: list = None) -> Optional[Signal]:
        if not self.enabled or kline_type != "K_1M" or not shared_buffer:
            return None
        closes, _, _, _ = _extract_ohlcv(shared_buffer)
        result = calc_macd(closes, self.fast, self.slow, self.signal)
        if result is None:
            return None
        _, _, hist = result
        prev_hist = self._prev_hist.get(code, 0.0)
        self._prev_hist[code] = hist

        close_price = closes[-1]
        hysteresis = max(close_price * self.hysteresis_pct, 0.001)

        if hist > hysteresis:
            if prev_hist <= hysteresis:  # 刚翻多
                self._prev_dir[code] = 1
                return Signal(code=code, action=SignalAction.BUY, price=close_price,
                              source=SignalSource.TECHNICAL,
                              reason=f"MACD 转多 (hist={hist:.4f})",
                              strategy=self.name)
        elif hist < -hysteresis:
            if prev_hist >= -hysteresis:  # 刚翻空
                self._prev_dir[code] = -1
                return Signal(code=code, action=SignalAction.SELL, price=close_price,
                              source=SignalSource.TECHNICAL,
                              reason=f"MACD 转空 (hist={hist:.4f})",
                              strategy=self.name)
        return None


# ============================================================
#  Factor 3: Bollinger Bands
# ============================================================

class BBFactorStrategy(BaseStrategy):
    """布林带均值回归因子 — MEAN_REVERSION + 波动率自适应"""

    name = "bb_factor"
    signal_channels = ["on_kline"]

    metadata = StrategyMetadata(
        factor_type=FactorType.MEAN_REVERSION,
        horizon=TimeHorizon.SWING,
        regime_affinity=["ranging"],
        risk_level=RiskLevel.LOW,
        required_klines=25,
        description="布林带极值检测 + 波动率自适应",
    )

    def __init__(self, cfg: dict = None):
        super().__init__(cfg or {})
        self.period = self.config.get("bb_period", 20)
        self.std_dev = self.config.get("bb_std", 2.0)
        self.vol_alpha = self.config.get("vol_adaptive_alpha", 1.5)
        self._vol_factor_hist: Dict[str, list] = {}

    def on_kline(self, code: str, kline: dict, kline_type: str,
                 shared_buffer: list = None) -> Optional[Signal]:
        if not self.enabled or kline_type != "K_1M" or not shared_buffer:
            return None
        closes, highs, lows, _ = _extract_ohlcv(shared_buffer)
        if len(closes) < self.period + 2:
            return None

        # 波动率自适应 σ
        vf = volatility_factor(highs, lows, closes, 20)
        if vf is not None:
            vol_hist = self._vol_factor_hist.setdefault(code, [])
            vol_hist.append(vf)
            if len(vol_hist) > 50:
                self._vol_factor_hist[code] = vol_hist[-50:]
            baseline = sorted(vol_hist)[len(vol_hist) // 2] if len(vol_hist) >= 5 else vf
            dyn_std = self.std_dev * volatility_adaptive_multiplier(vf, baseline, self.vol_alpha)
        else:
            dyn_std = self.std_dev

        bb = bollinger_bands(closes, self.period, dyn_std)
        if bb is None:
            return None
        upper, middle, lower = bb
        close = closes[-1]
        pctb = bollinger_pctb(close, upper, lower)

        if pctb <= 0.05:
            return Signal(code=code, action=SignalAction.BUY, price=close,
                          source=SignalSource.TECHNICAL,
                          reason=f"BB 下轨 (%B={pctb:.2f} σ={dyn_std:.1f})",
                          strategy=self.name)
        if pctb >= 0.95:
            return Signal(code=code, action=SignalAction.SELL, price=close,
                          source=SignalSource.TECHNICAL,
                          reason=f"BB 上轨 (%B={pctb:.2f} σ={dyn_std:.1f})",
                          strategy=self.name)
        return None


# ============================================================
#  Factor 4: SuperTrend
# ============================================================

class SuperTrendFactorStrategy(BaseStrategy):
    """SuperTrend 趋势因子 — MOMENTUM"""

    name = "st_factor"
    signal_channels = ["on_kline"]

    metadata = StrategyMetadata(
        factor_type=FactorType.MOMENTUM,
        horizon=TimeHorizon.SWING,
        regime_affinity=["trending"],
        risk_level=RiskLevel.MODERATE,
        required_klines=20,
        description="SuperTrend 方向检测 + 连续确认",
    )

    def __init__(self, cfg: dict = None):
        super().__init__(cfg or {})
        self.period = self.config.get("st_period", 10)
        self.multiplier = self.config.get("st_multiplier", 3.0)
        self.confirm_bars = self.config.get("supertrend_confirm_bars", 2)
        self._prev_dirs: Dict[str, list] = {}

    def on_kline(self, code: str, kline: dict, kline_type: str,
                 shared_buffer: list = None) -> Optional[Signal]:
        if not self.enabled or kline_type != "K_1M" or not shared_buffer:
            return None
        closes, highs, lows, _ = _extract_ohlcv(shared_buffer)
        if len(closes) < self.period + 5:
            return None
        st = calc_supertrend(highs, lows, closes, self.period, self.multiplier)
        if st is None:
            return None
        _, dir_str = st
        raw_dir = 1 if dir_str == "up" else -1

        dirs = self._prev_dirs.setdefault(code, [])
        dirs.append(raw_dir)
        if len(dirs) > self.confirm_bars:
            self._prev_dirs[code] = dirs[-self.confirm_bars:]

        if len(dirs) >= self.confirm_bars and all(d == dirs[-1] for d in dirs):
            if dirs[-1] == 1 and (len(dirs) < self.confirm_bars + 1 or dirs[-2] != 1):
                return Signal(code=code, action=SignalAction.BUY, price=closes[-1],
                              source=SignalSource.TECHNICAL,
                              reason=f"SuperTrend 转多 ({self.confirm_bars}K确认)",
                              strategy=self.name)
            if dirs[-1] == -1 and (len(dirs) < self.confirm_bars + 1 or dirs[-2] != -1):
                return Signal(code=code, action=SignalAction.SELL, price=closes[-1],
                              source=SignalSource.TECHNICAL,
                              reason=f"SuperTrend 转空 ({self.confirm_bars}K确认)",
                              strategy=self.name)
        return None


# ============================================================
#  Factor 5: MA Cross
# ============================================================

class MACrossFactorStrategy(BaseStrategy):
    """均线交叉因子 — MOMENTUM"""

    name = "ma_cross_factor"
    signal_channels = ["on_kline"]

    metadata = StrategyMetadata(
        factor_type=FactorType.MOMENTUM,
        horizon=TimeHorizon.SWING,
        regime_affinity=["trending"],
        risk_level=RiskLevel.LOW,
        required_klines=35,
        description="均线交叉 + 扩散度检测",
    )

    def __init__(self, cfg: dict = None):
        super().__init__(cfg or {})
        self.fast_period = self.config.get("ma_fast", 10)
        self.slow_period = self.config.get("ma_slow", 30)
        self.hysteresis_pct = self.config.get("ma_cross_hysteresis_pct", 0.003)
        self._prev_dir: Dict[str, int] = {}

    def on_kline(self, code: str, kline: dict, kline_type: str,
                 shared_buffer: list = None) -> Optional[Signal]:
        if not self.enabled or kline_type != "K_1M" or not shared_buffer:
            return None
        closes, _, _, _ = _extract_ohlcv(shared_buffer)
        if len(closes) < self.slow_period + 2:
            return None
        fast_ma = sum(closes[-self.fast_period:]) / self.fast_period
        slow_ma = sum(closes[-self.slow_period:]) / self.slow_period
        if slow_ma <= 0:
            return None

        diff_pct = abs(fast_ma - slow_ma) / slow_ma
        if diff_pct < self.hysteresis_pct:
            return None  # 均线粘合, 不投票

        direction = 1 if fast_ma > slow_ma else -1
        prev = self._prev_dir.get(code, 0)
        if direction != prev:
            self._prev_dir[code] = direction
            label = "金叉" if direction > 0 else "死叉"
            return Signal(
                code=code,
                action=SignalAction.BUY if direction > 0 else SignalAction.SELL,
                price=closes[-1],
                source=SignalSource.TECHNICAL,
                reason=f"MA {label} (差值={diff_pct:.2%})",
                strategy=self.name,
            )
        return None


# ============================================================
#  Factor 6: Volume Amplifier
# ============================================================

class VolumeAmplifierStrategy(BaseStrategy):
    """放量放大器因子 — MOMENTUM (价格方向确认)"""

    name = "vol_factor"
    signal_channels = ["on_kline"]

    metadata = StrategyMetadata(
        factor_type=FactorType.MOMENTUM,
        horizon=TimeHorizon.SWING,
        regime_affinity=["trending"],
        risk_level=RiskLevel.LOW,
        required_klines=25,
        description="放量 + 价格方向确认",
    )

    def __init__(self, cfg: dict = None):
        super().__init__(cfg or {})
        self.vol_ratio_threshold = self.config.get("vol_ratio", 1.5)
        self._min_interval_sec = self.config.get("vol_cooldown_sec", 300)
        self._last_signal_time: Dict[str, float] = {}
        self._last_direction: Dict[str, str] = {}

    def on_kline(self, code: str, kline: dict, kline_type: str,
                 shared_buffer: list = None) -> Optional[Signal]:
        if not self.enabled or kline_type != "K_1M" or not shared_buffer:
            return None
        closes, _, _, volumes = _extract_ohlcv(shared_buffer)
        if len(volumes) < 21 or len(closes) < 3:
            return None

        import time as _time
        avg_vol = sum(volumes[-21:-1]) / 20
        if avg_vol <= 0:
            return None
        vol_ratio = volumes[-1] / avg_vol
        if vol_ratio < self.vol_ratio_threshold:
            return None

        # 放量时看价格方向: 最近 3 根 K 线的净变化
        price_delta = closes[-1] - closes[-3]
        new_dir = "BUY" if price_delta > 0 else "SELL" if price_delta < 0 else None
        if new_dir is None:
            return None

        now = _time.time()
        last_time = self._last_signal_time.get(code, 0)
        last_dir = self._last_direction.get(code)

        # 冷却期内且方向相同 → 跳过 (防止连续投票噪音)
        if (now - last_time) < self._min_interval_sec and new_dir == last_dir:
            return None

        self._last_signal_time[code] = now
        self._last_direction[code] = new_dir

        action = SignalAction.BUY if price_delta > 0 else SignalAction.SELL
        label = "放量上涨" if price_delta > 0 else "放量下跌"
        return Signal(code=code, action=action, price=closes[-1],
                      source=SignalSource.TECHNICAL,
                      reason=f"{label} ({vol_ratio:.1f}x)",
                      strategy=self.name)
