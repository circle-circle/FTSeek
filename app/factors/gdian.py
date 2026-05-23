"""
GDIAN — GradientDirectionIndex (趋势方向因子)

衡量趋势方向强度，综合 MA 斜率 + ADX。

值域: value ∈ [0, 100]
  0-20:  无趋势 (震荡)
  20-35: 弱趋势 (门禁: GDIAN ≤ 35)
  35-50: 中等趋势
  50-100: 强趋势

score ∈ [-1, 1]
  正: 上升趋势
  负: 下降趋势
  绝对值: 趋势强度

对应门禁策略族群:
  5min 高动量:    GDIAN = 35 (紧趋势)
  15min 超卖反转: GDIAN ≤ 35-40
  日线深度反转:   GDIAN ≤ 25

参数空间:
  ma_period: [10, 20, 50]
  adx_period: [14]
  tight_threshold: [20, 25, 35, 45]
"""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional

from app.factors.base import BaseFactor, FactorParams, FactorTimeframe, FactorValue

logger = logging.getLogger(__name__)


class GDIANFactor(BaseFactor):
    """
    GradientDirectionIndex — 趋势方向因子
    
    计算逻辑:
    1. MA 斜率: (MA_now - MA_N_bars_ago) / MA_N_bars_ago → 趋势方向
    2. ADX: 趋势强度指标 (不关心方向，只关心强度)
    3. 综合: value = ADX 值, direction = MA 斜率方向
    
    ADX 计算:
    - +DM / -DM (方向运动)
    - TR (真实波幅)
    - +DI / -DI (方向指标)
    - DX = |+DI - -DI| / (+DI + -DI)
    - ADX = EMA(DX, period)
    """

    name = "GDIAN"
    timeframe = FactorTimeframe.MIN_15
    version = "1.0"

    param_space: Dict[str, List[Any]] = {
        "ma_period": [10, 20, 50],
        "adx_period": [14],
        "tight_threshold": [20, 25, 35, 45],
    }

    def compute(
        self,
        symbol: str,
        market_data: Dict[str, Any],
        ctx: Optional[Dict[str, Any]] = None,
    ) -> FactorValue:
        bars = market_data.get("bars", [])
        ma_period = self.params.get("ma_period", 20)
        adx_period = self.params.get("adx_period", 14)
        tight_threshold = self.params.get("tight_threshold", 35)

        min_bars = max(ma_period + 5, adx_period * 2 + 1)
        if len(bars) < min_bars:
            return FactorValue(
                name=self.name, value=0.0, score=0.0,
                direction_hint="NEUTRAL", confidence=0.0,
                metadata={"error": "insufficient_bars", "need": min_bars, "have": len(bars)},
                params_used=self.params.params,
            )

        closes = [b["close"] for b in bars]
        highs = [b["high"] for b in bars]
        lows = [b["low"] for b in bars]

        # ── 1. MA 斜率 ──
        ma_now = sum(closes[-ma_period:]) / ma_period
        ma_prev = sum(closes[-(ma_period * 2):-ma_period]) / ma_period if len(closes) >= ma_period * 2 else closes[0]
        ma_slope = self._safe_divide(ma_now - ma_prev, ma_prev) * 100  # 百分比

        # 方向: 正 = 上升, 负 = 下降
        trend_direction = 1.0 if ma_slope > 0 else -1.0

        # ── 2. ADX 计算 ──
        adx = self._compute_adx(highs, lows, closes, adx_period)

        # ── 综合 ──
        # value = ADX (趋势强度 0-100)
        value = self._clamp(adx, 0.0, 100.0)

        # score = ADX 强度 × 方向, 归一化到 [-1, 1]
        # 用 sigmoid 映射: ADX 25 → 0.5, ADX 50 → 0.9
        normalized_strength = 1.0 / (1.0 + math.exp(-0.08 * (adx - 25)))
        score = self._clamp(normalized_strength * trend_direction, -1.0, 1.0)

        # 方向判断
        if value <= tight_threshold:
            # 弱趋势 → 适合反转策略
            direction = "NEUTRAL"
        elif ma_slope > 0:
            direction = "LONG"
        else:
            direction = "SHORT"

        # 置信度: ADX 越高越确定
        confidence = self._clamp(adx / 60.0, 0.3, 1.0)

        return FactorValue(
            name=self.name,
            value=value,
            score=score,
            direction_hint=direction,
            confidence=confidence,
            metadata={
                "adx": round(adx, 2),
                "ma_slope_pct": round(ma_slope, 4),
                "ma_now": round(ma_now, 4),
                "trend_direction": trend_direction,
                "tight_threshold": tight_threshold,
                "is_tight_trend": value <= tight_threshold,
            },
            params_used=self.params.params,
        )

    @staticmethod
    def _compute_adx(
        highs: List[float],
        lows: List[float],
        closes: List[float],
        period: int,
    ) -> float:
        """
        计算 ADX (Average Directional Index)
        
        使用 Wilder 平滑法
        """
        if len(highs) < period * 2 + 1:
            return 25.0  # 默认中等趋势

        n = len(highs)

        # 计算 True Range, +DM, -DM
        tr_list = []
        plus_dm_list = []
        minus_dm_list = []

        for i in range(1, n):
            h = highs[i]
            l = lows[i]
            prev_c = closes[i - 1]

            tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
            tr_list.append(tr)

            up_move = h - highs[i - 1]
            down_move = lows[i - 1] - l

            plus_dm = up_move if (up_move > down_move and up_move > 0) else 0.0
            minus_dm = down_move if (down_move > up_move and down_move > 0) else 0.0

            plus_dm_list.append(plus_dm)
            minus_dm_list.append(minus_dm)

        # Wilder 平滑 (EMA with alpha = 1/period)
        atr = sum(tr_list[:period])
        plus_dm_smooth = sum(plus_dm_list[:period])
        minus_dm_smooth = sum(minus_dm_list[:period])

        dx_values = []

        for i in range(period, len(tr_list)):
            atr = atr - (atr / period) + tr_list[i]
            plus_dm_smooth = plus_dm_smooth - (plus_dm_smooth / period) + plus_dm_list[i]
            minus_dm_smooth = minus_dm_smooth - (minus_dm_smooth / period) + minus_dm_list[i]

            if atr == 0:
                continue

            plus_di = 100.0 * (plus_dm_smooth / atr)
            minus_di = 100.0 * (minus_dm_smooth / atr)

            di_sum = plus_di + minus_di
            if di_sum == 0:
                dx = 0.0
            else:
                dx = 100.0 * abs(plus_di - minus_di) / di_sum

            dx_values.append(dx)

        if not dx_values:
            return 25.0

        # ADX = EMA of DX
        if len(dx_values) < period:
            return sum(dx_values) / len(dx_values)

        adx = sum(dx_values[:period]) / period
        for dx in dx_values[period:]:
            adx = (adx * (period - 1) + dx) / period

        return adx
