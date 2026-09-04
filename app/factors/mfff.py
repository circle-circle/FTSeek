"""
MFFF (MomentumFlowFilterFactor) — 动量过滤因子

核心理念:
    入场时必须有充沛的正向动能支撑。
    综合 MACD 动量、成交量趋势、资金流方向 三个维度。

计算方法:
    MFFF = w_macd * macd_component + w_vol * vol_component + w_flow * flow_component

    其中:
    - macd_component: MACD histogram 归一化 (正值=多头动量)
    - vol_component: 成交量趋势 (近 5 根 vs 均量)
    - flow_component: 大单资金流方向

参数空间:
    macd_fast: [8, 12]
    macd_slow: [26]
    volume_weight: [0.3, 0.5, 0.7]
    threshold: [0.5, 1.0, 1.5, 2.0]
    w_macd: [0.3, 0.4, 0.5]
    w_vol: [0.2, 0.3]
    w_flow: [0.2, 0.3]

输出:
    score ∈ [-1, 1]:
      -1.0 = 极端负向动量 (空头强势)
       0.0 = 动量中性
      +1.0 = 极端正向动量 (多头强势)

    对于超卖反转策略:
        MFFF.score >= 1.0 意味着反转时已有正向动能确认

版本: 1.0 | 2026-05-23
"""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional

from .base import BaseFactor, FactorParams, FactorTimeframe, FactorValue

logger = logging.getLogger(__name__)


class MFFFFactor(BaseFactor):
    """动量过滤因子"""

    name = "MFFF"
    timeframe = FactorTimeframe.MIN_15
    version = "1.0"

    param_space = {
        "threshold": [0.5, 1.0, 1.5, 2.0],
        "w_macd": [0.3, 0.4, 0.5],
        "w_vol": [0.2, 0.3],
        "w_flow": [0.2, 0.3],
    }

    _DEFAULTS = {
        # DEB-20260903 #50: 1.0→0.3 — 原值使 L99 .get(...,0.3) 默认值被显式覆盖,
        # 有效阈值恒 1.0, 而 |raw_score|≤权重和=1.0 → 方向判定恒 NEUTRAL
        "threshold": 0.3,
        "w_macd": 0.4,
        "w_vol": 0.3,
        "w_flow": 0.3,
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
        计算动量过滤

        Args:
            symbol: 标的代码
            market_data: 必须包含:
                - macd, macd_signal, macd_hist: MACD 指标
                - volume, avg_volume: 成交量
                可选:
                - bars: K线序列 (用于量趋势)
                - net_capital_flow: 净流入
                - large_order_ratio: 大单占比
            ctx: 未使用
        """
        md = market_data
        w_macd = self.params.get("w_macd", 0.4)
        w_vol = self.params.get("w_vol", 0.3)
        w_flow = self.params.get("w_flow", 0.3)
        # DEB-20260903 #50: threshold 默认 1.0→0.3 与方向判定对齐
        # (原默认 1.0 且权重和=1.0 → |raw_score|≤1 → above_threshold 几乎恒 False)
        threshold = float(self.params.get("threshold", 0.3))

        # 1. MACD 动量分量
        macd_component = self._compute_macd_component(md)

        # 2. 成交量趋势分量
        vol_component = self._compute_vol_component(md)

        # 3. 资金流分量
        flow_component = self._compute_flow_component(md)

        # 综合评分
        raw_score = (w_macd * macd_component
                     + w_vol * vol_component
                     + w_flow * flow_component)

        # 方向判定
        # DEB-20260903 #50: 使用 threshold 参数 (原硬编码 0.3 使参数无效)
        if raw_score >= threshold:
            direction = "LONG"
            confidence = min(1.0, abs(raw_score))
        elif raw_score <= -threshold:
            direction = "SHORT"
            confidence = min(1.0, abs(raw_score))
        else:
            direction = "NEUTRAL"
            confidence = 1.0 - abs(raw_score)

        return FactorValue(
            name=self.name,
            value=raw_score,
            score=raw_score,
            direction_hint=direction,
            confidence=confidence,
            params_used={
                "threshold": threshold,
                "w_macd": w_macd, "w_vol": w_vol, "w_flow": w_flow,
            },
            metadata={
                "macd_component": round(macd_component, 4),
                "vol_component": round(vol_component, 4),
                "flow_component": round(flow_component, 4),
                "above_threshold": abs(raw_score) >= threshold,
            },
        )

    def _compute_macd_component(self, md: Dict[str, Any]) -> float:
        """MACD histogram 归一化"""
        macd = float(md.get("macd", 0))
        macd_signal = float(md.get("macd_signal", 0))
        macd_hist = float(md.get("macd_hist", macd - macd_signal))
        price = float(md.get("price", md.get("close", 1)))

        if price <= 0:
            return 0.0

        # MACD histogram 占价格的百分比
        hist_pct = macd_hist / price * 100

        # 归一化: ±2% 为极端
        component = max(-1.0, min(1.0, hist_pct / 2.0))

        # MACD 交叉信号加成
        if macd > macd_signal and macd_hist > 0:
            component = min(1.0, component * 1.2)  # 多头交叉加成
        elif macd < macd_signal and macd_hist < 0:
            component = max(-1.0, component * 1.2)  # 空头交叉加成

        return component

    def _compute_vol_component(self, md: Dict[str, Any]) -> float:
        """成交量趋势"""
        volume = float(md.get("volume", 0))
        avg_volume = float(md.get("avg_volume", 1))
        bars = md.get("bars", [])

        # 基础量比
        if avg_volume > 0:
            vol_ratio = volume / avg_volume
        else:
            vol_ratio = 1.0

        # 量趋势: 近几根 K 线的量变化方向
        vol_trend = 0.0
        if len(bars) >= 3:
            vols = [float(b.get("volume", 0)) for b in bars[-5:]]
            if vols[0] > 0:
                # 线性趋势斜率
                n = len(vols)
                x_mean = (n - 1) / 2
                y_mean = sum(vols) / n
                num = sum((i - x_mean) * (vols[i] - y_mean) for i in range(n))
                den = sum((i - x_mean) ** 2 for i in range(n))
                if den > 0 and y_mean > 0:
                    vol_trend = (num / den) / y_mean * 5  # 归一化

        # 综合: 量比偏离 + 量趋势
        vol_deviation = max(-1.0, min(1.0, (vol_ratio - 1.0)))
        component = 0.6 * vol_deviation + 0.4 * max(-1.0, min(1.0, vol_trend))

        return component

    def _compute_flow_component(self, md: Dict[str, Any]) -> float:
        """资金流方向"""
        # 直接提供净流入
        net_flow = md.get("net_capital_flow", None)
        if net_flow is not None:
            try:
                net_flow = float(net_flow)
                # 归一化: ±1M 为极端
                return max(-1.0, min(1.0, net_flow / 1_000_000))
            except (TypeError, ValueError):
                pass

        # 大单占比
        large_ratio = md.get("large_order_ratio", None)
        if large_ratio is not None:
            try:
                large_ratio = float(large_ratio)
                # 大单占比 > 0.3 = 强流入
                return max(-1.0, min(1.0, (large_ratio - 0.2) * 5))
            except (TypeError, ValueError):
                pass

        # 无法获取资金流数据 → 用量价相关性近似
        bars = md.get("bars", [])
        if len(bars) >= 3:
            # 简化: 价格上涨 + 放量 = 正向流入
            price_change = 0
            vol_change = 0
            for i in range(1, min(4, len(bars))):
                c = float(bars[i].get("close", 0))
                pc = float(bars[i - 1].get("close", 0))
                v = float(bars[i].get("volume", 0))
                pv = float(bars[i - 1].get("volume", 0))
                if pc > 0 and pv > 0:
                    price_change += (c - pc) / pc
                    vol_change += (v - pv) / pv

            # 量价正相关 = 正向流入
            if price_change != 0:
                correlation = (1 if (price_change > 0) == (vol_change > 0) else -1)
                strength = min(1.0, abs(price_change) * 50)
                return correlation * strength * 0.5

        return 0.0


FACTOR_CLASS = MFFFFactor
