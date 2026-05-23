"""
SSS — SuperSoldScore (超卖深度因子)

衡量标的超卖程度，综合 RSI + Bollinger Band 位置 + 成交量异常。

值域: score ∈ [-2, 2]
  -2: 极度超卖 (强烈买入信号)
  -1: 中度超卖
   0: 中性
  +1: 中度超买
  +2: 极度超买 (强烈卖出信号)

对应门禁策略族群:
  5min 高动量:    SSS = -1
  15min 超卖反转: SSS ∈ [-1.5, 1]
  日线深度反转:   SSS = -2

参数空间:
  rsi_period: [7, 14, 21]
  bb_period: [20]
  bb_std: [1.5, 2.0, 2.5]
  volume_lookback: [20]
"""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional

from app.factors.base import BaseFactor, FactorParams, FactorTimeframe, FactorValue

logger = logging.getLogger(__name__)


class SSSFactor(BaseFactor):
    """
    SuperSoldScore — 超卖深度因子
    
    计算逻辑:
    1. RSI 偏离度: (RSI - 50) / 50 → [-1, 1]，反转符号使得超卖为正
    2. BB 位置: (close - lower_band) / (upper_band - lower_band) → [0, 1]
       反转并扩展到 [-1, 1]: 0 = 下轨 (超卖), 1 = 上轨 (超买)
    3. 成交量异常: 当前成交量 / 过去 N 根均值 → 异常度
    
    综合: score = w_rsi * rsi_signal + w_bb * bb_signal + w_vol * vol_signal
    """

    name = "SSS"
    timeframe = FactorTimeframe.MIN_15
    version = "1.0"

    param_space: Dict[str, List[Any]] = {
        "rsi_period": [7, 14, 21],
        "bb_period": [20],
        "bb_std": [1.5, 2.0, 2.5],
        "volume_lookback": [20],
    }

    # 默认权重
    _W_RSI = 0.45
    _W_BB = 0.40
    _W_VOL = 0.15

    def compute(
        self,
        symbol: str,
        market_data: Dict[str, Any],
        ctx: Optional[Dict[str, Any]] = None,
    ) -> FactorValue:
        bars = market_data.get("bars", [])
        if len(bars) < max(
            self.params.get("rsi_period", 14) + 1,
            self.params.get("bb_period", 20),
            self.params.get("volume_lookback", 20),
        ):
            return FactorValue(
                name=self.name, value=0.0, score=0.0,
                direction_hint="NEUTRAL", confidence=0.0,
                metadata={"error": "insufficient_bars"},
                params_used=self.params.params,
            )

        closes = [b["close"] for b in bars]
        volumes = [b.get("volume", 0) for b in bars]

        rsi_period = self.params.get("rsi_period", 14)
        bb_period = self.params.get("bb_period", 20)
        bb_std = self.params.get("bb_std", 2.0)
        vol_lookback = self.params.get("volume_lookback", 20)

        # ── 1. RSI 偏离度 ──
        rsi = self._compute_rsi(closes, rsi_period)
        # RSI 30 = 超卖 → score 正; RSI 70 = 超买 → score 负
        rsi_signal = self._clamp((50 - rsi) / 50, -1.0, 1.0)

        # ── 2. Bollinger Band 位置 ──
        bb_pct = self._compute_bb_position(closes, bb_period, bb_std)
        # bb_pct 0 = 下轨 (超卖), 1 = 上轨 (超买)
        bb_signal = self._clamp(1.0 - 2.0 * bb_pct, -1.0, 1.0)

        # ── 3. 成交量异常 ──
        vol_ratio = self._compute_volume_ratio(volumes, vol_lookback)
        # 放量超卖增加信号强度 (方向由 rsi/bb 决定，vol 只做放大)
        vol_signal = min(vol_ratio - 1.0, 1.0)  # 正常量 = 0, 放量 > 0

        # ── 综合评分 ──
        raw_score = (
            self._W_RSI * rsi_signal +
            self._W_BB * bb_signal +
            self._W_VOL * vol_signal * (1.0 if rsi_signal > 0 else -1.0)
        )

        # 映射到 [-2, 2] 范围 (与门禁策略的 SSS 值对齐)
        final_score = self._clamp(raw_score * 2.0, -2.0, 2.0)

        # 方向判断
        if final_score >= 0.5:
            direction = "LONG"
        elif final_score <= -0.5:
            direction = "SHORT"
        else:
            direction = "NEUTRAL"

        # 置信度: 多个子信号一致时更高
        agreement = sum(1 for s in [rsi_signal, bb_signal] if (s > 0) == (raw_score > 0))
        confidence = self._clamp(0.5 + 0.25 * agreement, 0.0, 1.0)

        return FactorValue(
            name=self.name,
            value=final_score,
            score=self._clamp(final_score / 2.0, -1.0, 1.0),  # 归一化到 [-1, 1]
            direction_hint=direction,
            confidence=confidence,
            metadata={
                "rsi": round(rsi, 2),
                "rsi_signal": round(rsi_signal, 4),
                "bb_pct": round(bb_pct, 4),
                "bb_signal": round(bb_signal, 4),
                "vol_ratio": round(vol_ratio, 2),
                "vol_signal": round(vol_signal, 4),
                "raw_score": round(raw_score, 4),
            },
            params_used=self.params.params,
        )

    # ── 技术指标计算 (纯函数) ──

    @staticmethod
    def _compute_rsi(closes: List[float], period: int) -> float:
        """计算 RSI (Wilder 平滑)"""
        if len(closes) < period + 1:
            return 50.0  # 默认中性

        deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains = [max(d, 0) for d in deltas[-period:]]
        losses = [abs(min(d, 0)) for d in deltas[-period:]]

        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period

        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    @staticmethod
    def _compute_bb_position(
        closes: List[float], period: int, num_std: float
    ) -> float:
        """计算 Bollinger Band 位置 (0 = 下轨, 1 = 上轨)"""
        if len(closes) < period:
            return 0.5  # 默认中轨

        window = closes[-period:]
        mean = sum(window) / len(window)
        variance = sum((x - mean) ** 2 for x in window) / len(window)
        std = math.sqrt(variance)

        if std == 0:
            return 0.5

        upper = mean + num_std * std
        lower = mean - num_std * std
        current = closes[-1]

        if upper == lower:
            return 0.5

        pct = (current - lower) / (upper - lower)
        return max(0.0, min(1.0, pct))

    @staticmethod
    def _compute_volume_ratio(volumes: List[float], lookback: int) -> float:
        """计算成交量比率 (当前 / 过去 N 根均值)"""
        if len(volumes) < lookback + 1:
            return 1.0

        current_vol = volumes[-1]
        past_vols = volumes[-(lookback + 1):-1]
        avg_vol = sum(past_vols) / len(past_vols) if past_vols else 1.0

        if avg_vol == 0:
            return 1.0

        return current_vol / avg_vol
