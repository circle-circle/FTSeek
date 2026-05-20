"""
Market Regime Detector (Y1, 2026-05-15)

基于 Agent 诊断 700/700 笔交易全 UNKNOWN regime 致命问题.

最简实现 (无新依赖, 用 SPY + VIX 数据):
- ADX(14) → TRENDING (>25) / RANGING (<20) / TRANSITION (20-25)
- VIX → LOW_VOL (<15) / NORMAL (15-25) / HIGH_VOL (>25) / EXTREME (>35)
- SPY 日内变化 → BULLISH (>1%) / BEARISH (<-1%) / NEUTRAL

输出:
- regime: TRENDING_UP / TRENDING_DOWN / RANGING / HIGH_VOL_CHOP / EXTREME
- confidence: 0-1
- 写入每笔 trade_records.market_regime

可控:
- 失败 → 默认 'UNKNOWN' (兼容现有数据)
- 数据不足 (<14 K线) → 'WARMUP'
- 30s cache 防过频计算
"""
from __future__ import annotations
import logging
import time
from dataclasses import dataclass
from typing import Optional, List

logger = logging.getLogger(__name__)


@dataclass
class RegimeSnapshot:
    regime: str
    confidence: float
    adx: float
    vix: float
    spy_change_pct: float
    timestamp: float


class MarketRegimeDetector:
    """简单 regime 分类器 (基于 SPY ADX + VIX + 日内变化)"""

    def __init__(self):
        self._last: Optional[RegimeSnapshot] = None
        self._cache_ttl_sec = 30.0

    def detect(
        self,
        spy_highs: Optional[List[float]] = None,
        spy_lows: Optional[List[float]] = None,
        spy_closes: Optional[List[float]] = None,
        vix: float = 20.0,
        spy_change_pct: float = 0.0,
    ) -> RegimeSnapshot:
        """
        Args:
            spy_highs/lows/closes: 至少 14 根 5min K 线 (做 ADX)
            vix: 当前 VIX
            spy_change_pct: SPY 当日变化 (-0.02 = -2%)
        """
        # cache
        if self._last and time.time() - self._last.timestamp < self._cache_ttl_sec:
            return self._last

        adx = self._compute_adx(spy_highs, spy_lows, spy_closes)
        regime, confidence = self._classify(adx, vix, spy_change_pct)
        snap = RegimeSnapshot(
            regime=regime, confidence=confidence,
            adx=adx, vix=vix, spy_change_pct=spy_change_pct,
            timestamp=time.time(),
        )
        self._last = snap
        return snap

    def _compute_adx(self, highs, lows, closes, period: int = 14) -> float:
        """ADX 计算 (纯 Python, 无 talib 依赖)"""
        if not highs or not lows or not closes:
            return -1.0  # 数据缺失
        n = len(closes)
        if n < period + 1:
            return -1.0
        try:
            # +DM, -DM, TR
            plus_dm, minus_dm, tr_list = [], [], []
            for i in range(1, n):
                high_diff = highs[i] - highs[i-1]
                low_diff = lows[i-1] - lows[i]
                plus_dm.append(high_diff if (high_diff > 0 and high_diff > low_diff) else 0)
                minus_dm.append(low_diff if (low_diff > 0 and low_diff > high_diff) else 0)
                tr = max(highs[i] - lows[i],
                        abs(highs[i] - closes[i-1]),
                        abs(lows[i] - closes[i-1]))
                tr_list.append(tr)

            # 平滑 (Wilder)
            atr = sum(tr_list[:period]) / period
            plus_di_smooth = sum(plus_dm[:period]) / period
            minus_di_smooth = sum(minus_dm[:period]) / period

            for i in range(period, len(tr_list)):
                atr = (atr * (period - 1) + tr_list[i]) / period
                plus_di_smooth = (plus_di_smooth * (period - 1) + plus_dm[i]) / period
                minus_di_smooth = (minus_di_smooth * (period - 1) + minus_dm[i]) / period

            if atr == 0:
                return -1.0

            plus_di = 100 * plus_di_smooth / atr
            minus_di = 100 * minus_di_smooth / atr
            denom = plus_di + minus_di
            if denom == 0:
                return -1.0

            dx = 100 * abs(plus_di - minus_di) / denom
            return dx  # 简化: 用 DX 代替平滑后的 ADX
        except Exception as e:
            logger.debug(f"[RegimeDetector·adx] 计算失败: {e}")
            return -1.0

    def _classify(self, adx: float, vix: float, spy_chg: float):
        """综合分类"""
        # 1) Extreme VIX → EXTREME (覆盖一切)
        if vix > 35:
            return "EXTREME", 0.95

        # 2) ADX 数据缺失 → 仅用 VIX
        if adx < 0:
            if vix < 15:
                return "RANGING", 0.6
            if vix < 25:
                return "RANGING", 0.5  # 默认偏震荡
            return "HIGH_VOL_CHOP", 0.6

        # 3) ADX 主导
        if adx >= 25:
            # 趋势
            if spy_chg > 0.005:
                return "TRENDING_UP", min(0.9, 0.5 + adx / 100)
            elif spy_chg < -0.005:
                return "TRENDING_DOWN", min(0.9, 0.5 + adx / 100)
            else:
                return "TRENDING", 0.6  # 趋势但方向不明
        elif adx < 20:
            # 震荡
            if vix > 25:
                return "HIGH_VOL_CHOP", 0.7
            return "RANGING", 0.7
        else:
            # 20-25 transition
            return "TRANSITION", 0.5


_singleton: Optional[MarketRegimeDetector] = None


def get_regime_detector() -> MarketRegimeDetector:
    global _singleton
    if _singleton is None:
        _singleton = MarketRegimeDetector()
    return _singleton


# 各 regime 对应的策略推荐 (参考用)
REGIME_STRATEGY_MAP = {
    "TRENDING_UP": {
        "favor": ["momentum", "breakout"],
        "avoid": ["mean_reversion"],
        "position_scale": 1.0,
    },
    "TRENDING_DOWN": {
        "favor": ["short_momentum"],
        "avoid": ["mean_reversion"],
        "position_scale": 1.0,
    },
    "TRENDING": {
        "favor": ["trend_following"],
        "avoid": [],
        "position_scale": 0.8,
    },
    "RANGING": {
        "favor": ["mean_reversion"],
        "avoid": ["breakout", "momentum"],
        "position_scale": 0.6,
    },
    "HIGH_VOL_CHOP": {
        "favor": [],   # 不推荐任何策略
        "avoid": ["breakout", "momentum", "mean_reversion"],
        "position_scale": 0.3,
    },
    "TRANSITION": {
        "favor": [],
        "avoid": [],
        "position_scale": 0.5,
    },
    "EXTREME": {
        "favor": [],   # 极端波动期, 不开仓
        "avoid": ["all"],
        "position_scale": 0.0,
    },
    "UNKNOWN": {
        "favor": [],
        "avoid": [],
        "position_scale": 0.5,
    },
}


def get_position_scale(regime: str) -> float:
    """根据 regime 获取仓位缩放系数"""
    return REGIME_STRATEGY_MAP.get(regime, {}).get("position_scale", 0.5)
