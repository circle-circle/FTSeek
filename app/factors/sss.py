"""
SSS (SuperSoldScore) — 超卖深度因子

核心理念:
    量化标的当前的超卖程度。值越负表示超卖越深，反转概率越高。

计算方法:
    综合 RSI、Bollinger Band %B、成交量萎缩 三个维度评估超卖深度。

    SSS = w_rsi * rsi_component + w_bb * bb_component + w_vol * vol_component

    其中:
    - rsi_component: (RSI - 50) / 50, 范围 [-1, 1], RSI=20 → -0.6
    - bb_component: (%B - 0.5) * 2, 范围 [-1, 1], %B=0 → -1.0
    - vol_component: -(1 - volume/avg_volume), 量缩越大越超卖

参数空间:
    rsi_period: [7, 14, 21]
    bb_period: [20]
    bb_std: [1.5, 2.0, 2.5]
    threshold: [-2, -1.5, -1, -0.5, 0, 0.5, 1]
    w_rsi: [0.4, 0.5]
    w_bb: [0.3, 0.4]
    w_vol: [0.1, 0.2]

输出:
    score ∈ [-1, 1]:
      -1.0 = 极端超卖 (RSI<20, 低于布林下轨, 量缩)
       0.0 = 中性
      +1.0 = 极端超买

版本: 1.0 | 2026-05-23
"""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional

from .base import BaseFactor, FactorParams, FactorTimeframe, FactorValue

logger = logging.getLogger(__name__)


class SSSFactor(BaseFactor):
    """超卖深度因子"""

    name = "SSS"
    timeframe = FactorTimeframe.MIN_15
    version = "1.0"

    param_space = {
        "threshold": [-2, -1.5, -1, -0.5, 0, 0.5, 1],
        "w_rsi": [0.4, 0.5],
        "w_bb": [0.3, 0.4],
        "w_vol": [0.1, 0.2],
    }

    # 默认参数
    _DEFAULTS = {
        # DEB-20260903 #52: -1.0→-0.5 — 原值 abs 后=1.0 使 L141 .get(...,0.5) 默认值被覆盖,
        # 有效阈值恒 1.0, 而 |raw_score|≤权重和=1.0 → 方向判定恒 NEUTRAL
        "threshold": -0.5,
        "w_rsi": 0.5,
        "w_bb": 0.35,
        "w_vol": 0.15,
    }

    def __init__(self, params: Optional[FactorParams] = None):
        if params is None:
            params = FactorParams(params=dict(self._DEFAULTS))
        else:
            # 填充缺失参数
            for k, v in self._DEFAULTS.items():
                if k not in params.params:
                    params.params[k] = v
        super().__init__(params)

    def compute(self, symbol: str, market_data: Dict[str, Any],
                ctx: Optional[Dict[str, Any]] = None) -> FactorValue:
        """
        计算超卖深度

        Args:
            symbol: 标的代码
            market_data: 必须包含:
                - rsi_14 (或 rsi_N): RSI 值
                - bollinger_upper, bollinger_lower, price: 计算 %B
                - volume, avg_volume: 量比
            ctx: 未使用
        """
        md = market_data
        w_rsi = self.params.get("w_rsi", 0.5)
        w_bb = self.params.get("w_bb", 0.35)
        w_vol = self.params.get("w_vol", 0.15)

        # 1. RSI 分量
        rsi = md.get("rsi_14", md.get("rsi", 50.0))
        try:
            rsi = float(rsi)
        except (TypeError, ValueError):
            rsi = 50.0
        # 归一化到 [-1, 1]: RSI=0 → -1, RSI=50 → 0, RSI=100 → 1
        rsi_component = (rsi - 50.0) / 50.0
        rsi_component = max(-1.0, min(1.0, rsi_component))

        # 2. Bollinger %B 分量
        price = float(md.get("price", md.get("close", 0)))
        bb_upper = float(md.get("bollinger_upper", 0))
        bb_lower = float(md.get("bollinger_lower", 0))

        if bb_upper > bb_lower and bb_upper > 0:
            pct_b = (price - bb_lower) / (bb_upper - bb_lower)
            # %B ∈ [0, 1], 归一化到 [-1, 1]
            bb_component = (pct_b - 0.5) * 2.0
            bb_component = max(-1.0, min(1.0, bb_component))
        else:
            bb_component = 0.0
            pct_b = 0.5

        # 3. 量缩分量 (量缩越大 → 超卖信号越强)
        volume = float(md.get("volume", 0))
        avg_volume = float(md.get("avg_volume", 1))
        if avg_volume > 0:
            vol_ratio = volume / avg_volume
            # vol_ratio=0.3 → component=0.7 (缩量=超卖)
            # vol_ratio=2.0 → component=-1.0 (放量=不超卖)
            vol_component = max(-1.0, min(1.0, 1.0 - vol_ratio))
        else:
            vol_ratio = 1.0
            vol_component = 0.0

        # 综合评分
        raw_score = (w_rsi * rsi_component
                     + w_bb * bb_component
                     + w_vol * vol_component)

        # 注意: SSS 是"超卖"因子，score 越负越超卖
        # 但 direction_hint 应该是反直觉的: 超卖 → 看多(反转)
        # DEB-20260903 #52: threshold 参数参与方向判定 (原仅写 params_used 不生效);
        # 默认 0.5 保持原行为。注意: SSS 极性与 MFFF 相反 (负=看多反转),
        # 聚合层不得对两因子 score 做同号加权 (见 params_used["polarity"])
        # DEB-20260904 #157: threshold=0 是合法参数 (恒触发) — `or 0.5` 会把 0.0
        # 静默改回 0.5, 参数空间中的 0 实际无效。仅 None/缺失时用默认。
        _raw_thr = self.params.get("threshold", 0.5)
        _thr = abs(float(_raw_thr)) if _raw_thr is not None else 0.5
        if raw_score <= -_thr:
            direction = "LONG"   # 超卖 → 做多反转
            confidence = min(1.0, abs(raw_score))
        elif raw_score >= _thr:
            direction = "SHORT"  # 超买 → 做空反转
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
            params_used={"w_rsi": w_rsi, "w_bb": w_bb, "w_vol": w_vol,
                         "threshold": _thr,
                         # DEB-20260903 #52: 显式标注极性 — SSS 为反转因子 (负分=看多),
                         # 与动量类因子 (MFFF 正分=看多) 极性相反, 聚合时需感知
                         "polarity": "reversal_negative_is_long"},
            metadata={
                "rsi": round(rsi, 2),
                "rsi_component": round(rsi_component, 4),
                "pct_b": round(pct_b, 4),
                "bb_component": round(bb_component, 4),
                "vol_ratio": round(vol_ratio, 3),
                "vol_component": round(vol_component, 4),
            },
        )


# 模块级导出 (供 auto_discover 使用)
FACTOR_CLASS = SSSFactor
