"""
GDIAN (GradientDirectionIndex) — 趋势方向强度因子

核心理念:
    量化当前趋势的方向性和强度。
    值越小说明趋势越弱/越混乱 (适合反转策略入场)。
    值越大说明趋势越明确 (适合趋势跟随策略)。

计算方法:
    综合 MA 斜率、ADX、价格相对 MA 位置 评估趋势方向。

    GDIAN = w_slope * slope_component + w_adx * adx_component + w_pos * position_component

    其中:
    - slope_component: MA20 斜率 (归一化), 正=上升趋势
    - adx_component: ADX 强度 (归一化), 高=趋势明确
    - position_component: 价格相对 MA50 的偏离度

参数空间:
    ma_fast: [10, 20]
    ma_slow: [50]
    adx_period: [14]
    tight_threshold: [20, 25, 35, 45]  — 趋势"紧"的阈值
    w_slope: [0.3, 0.4]
    w_adx: [0.3, 0.4]
    w_pos: [0.2, 0.3]

输出:
    value: 0-100 (趋势强度综合值)
    score ∈ [-1, 1]:
      -1.0 = 强下行趋势
       0.0 = 无趋势 / 震荡
      +1.0 = 强上行趋势

    对于超卖反转策略:
        GDIAN.value <= 35 意味着趋势较弱，适合反转入场

版本: 1.0 | 2026-05-23
"""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional

from .base import BaseFactor, FactorParams, FactorTimeframe, FactorValue

logger = logging.getLogger(__name__)


class GDIANFactor(BaseFactor):
    """趋势方向强度因子"""

    name = "GDIAN"
    timeframe = FactorTimeframe.MIN_15
    version = "1.0"

    param_space = {
        "tight_threshold": [20, 25, 35, 45],
        "w_slope": [0.3, 0.4],
        "w_adx": [0.3, 0.4],
        "w_pos": [0.2, 0.3],
    }

    _DEFAULTS = {
        "tight_threshold": 35,
        "w_slope": 0.35,
        "w_adx": 0.35,
        "w_pos": 0.30,
    }

    def __init__(self, params: Optional[FactorParams] = None):
        if params is None:
            params = FactorParams(params=dict(self._DEFAULTS))
        else:
            for k, v in self._DEFAULTS.items():
                if k not in params.params:
                    params.params[k] = v
        super().__init__(params)

    def compute(self, symbol: str, market_data: Dict[str, Any],
                ctx: Optional[Dict[str, Any]] = None) -> FactorValue:
        """
        计算趋势方向强度

        Args:
            symbol: 标的代码
            market_data: 必须包含:
                - ma_5, ma_20, ma_50: 均线
                - price: 当前价格
                - bars: 最近 N 根 K 线 (用于计算 MA 斜率)
                可选:
                - adx: ADX 值 (若无则从 bars 估算)
            ctx: 未使用
        """
        md = market_data
        w_slope = self.params.get("w_slope", 0.35)
        w_adx = self.params.get("w_adx", 0.35)
        w_pos = self.params.get("w_pos", 0.30)
        tight_threshold = self.params.get("tight_threshold", 35)

        price = float(md.get("price", md.get("close", 0)))
        ma_20 = float(md.get("ma_20", 0))
        ma_50 = float(md.get("ma_50", 0))

        # 1. MA 斜率分量 (从 bars 计算 MA20 的变化率)
        slope_component = self._compute_slope(md)

        # 2. ADX 分量
        adx = md.get("adx", None)
        if adx is not None:
            try:
                adx = float(adx)
            except (TypeError, ValueError):
                adx = self._estimate_adx(md)
        else:
            adx = self._estimate_adx(md)

        # ADX 归一化: 0-25=弱, 25-50=中, 50+=强
        # 归一化到 [0, 1]
        adx_norm = min(1.0, adx / 50.0)
        adx_component = adx_norm

        # 3. 价格相对 MA50 的偏离度
        if ma_50 > 0:
            deviation = (price - ma_50) / ma_50 * 100  # 百分比
            # 归一化: ±5% 以内为中性，±15% 为极端
            position_component = max(-1.0, min(1.0, deviation / 15.0))
        else:
            deviation = 0.0
            position_component = 0.0

        # 综合: value 范围 0-100
        # slope + position 给出方向，adx 给出强度
        direction_signal = w_slope * slope_component + w_pos * position_component
        trend_strength = adx_component * 100  # 0-100

        # 综合值: 趋势强度 × 方向
        raw_value = trend_strength * abs(direction_signal)

        # 方向判定
        if direction_signal > 0.1:
            trend_dir = "up"
            score = min(1.0, raw_value / 50.0)
        elif direction_signal < -0.1:
            trend_dir = "down"
            score = -min(1.0, raw_value / 50.0)
        else:
            trend_dir = "neutral"
            score = 0.0

        # 方向提示: 趋势弱 → 适合反转; 趋势强 → 适合跟随
        is_tight = raw_value <= tight_threshold

        if is_tight and score < 0:
            direction_hint = "LONG"   # 弱下跌趋势 → 反转做多
        elif is_tight and score > 0:
            direction_hint = "SHORT"  # 弱上涨趋势 → 反转做空
        elif not is_tight and score > 0:
            direction_hint = "LONG"   # 强上涨趋势 → 跟随做多
        elif not is_tight and score < 0:
            direction_hint = "SHORT"  # 强下跌趋势 → 跟随做空
        else:
            direction_hint = "NEUTRAL"

        confidence = min(1.0, adx_norm + abs(direction_signal))

        return FactorValue(
            name=self.name,
            value=round(raw_value, 2),
            score=score,
            direction_hint=direction_hint,
            confidence=confidence,
            params_used={
                "tight_threshold": tight_threshold,
                "w_slope": w_slope, "w_adx": w_adx, "w_pos": w_pos,
            },
            metadata={
                "slope_component": round(slope_component, 4),
                "adx": round(adx, 2),
                "adx_component": round(adx_component, 4),
                "deviation_pct": round(deviation, 2),
                "position_component": round(position_component, 4),
                "trend_direction": trend_dir,
                "is_tight": is_tight,
                "raw_value": round(raw_value, 2),
            },
        )

    def _compute_slope(self, md: Dict[str, Any]) -> float:
        """从 bars 或 MA 值计算 MA20 斜率 (归一化)"""
        bars = md.get("bars", [])
        ma_20 = float(md.get("ma_20", 0))
        ma_5 = float(md.get("ma_5", 0))

        if ma_20 > 0 and ma_5 > 0:
            # 快速近似: MA5 在 MA20 之上 → 上升斜率
            cross_signal = (ma_5 - ma_20) / ma_20 * 100
            return max(-1.0, min(1.0, cross_signal / 3.0))

        if len(bars) >= 5:
            # 从 bars 估算斜率 (线性回归斜率)
            closes = [float(b.get("close", 0)) for b in bars[-5:]]
            if closes and closes[0] > 0:
                n = len(closes)
                x_mean = (n - 1) / 2
                y_mean = sum(closes) / n
                numerator = sum((i - x_mean) * (closes[i] - y_mean) for i in range(n))
                denominator = sum((i - x_mean) ** 2 for i in range(n))
                if denominator > 0:
                    slope = numerator / denominator
                    # 归一化: slope / price * 100 → 百分比变化率
                    norm_slope = slope / closes[0] * 100 * 10  # 放大
                    return max(-1.0, min(1.0, norm_slope))

        return 0.0

    def _estimate_adx(self, md: Dict[str, Any]) -> float:
        """从价格波动估算 ADX (简化版)"""
        bars = md.get("bars", [])
        if len(bars) < 5:
            return 20.0  # 默认中性

        # 用 True Range 的变异系数近似 ADX
        trs = []
        for i in range(1, len(bars)):
            h = float(bars[i].get("high", 0))
            l = float(bars[i].get("low", 0))
            pc = float(bars[i - 1].get("close", 0))
            if pc > 0:
                tr = max(h - l, abs(h - pc), abs(l - pc))
                trs.append(tr / pc * 100)

        if not trs:
            return 20.0

        mean_tr = sum(trs) / len(trs)
        if mean_tr <= 0:
            return 20.0

        # 方向一致性: 连续上涨/下跌天数越多 → ADX 越高
        directions = []
        for i in range(1, len(bars)):
            c = float(bars[i].get("close", 0))
            pc = float(bars[i - 1].get("close", 0))
            if pc > 0:
                directions.append(1 if c > pc else -1)

        if directions:
            # 一致性: 全正=1.0, 全负=1.0, 混合=0.0
            consistency = abs(sum(directions)) / len(directions)
        else:
            consistency = 0.5

        # ADX ≈ volatility * consistency * scale
        adx_est = mean_tr * consistency * 15
        return max(5.0, min(60.0, adx_est))


FACTOR_CLASS = GDIANFactor
