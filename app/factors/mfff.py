"""
MFFF — MomentumFlowFilterFactor (动量过滤因子)

衡量价格动量 + 资金流向，综合 MACD + 成交量比 + 价格变化率。

值域: score ∈ [-2, 2]
  ≥ 2.0: 强正向动量 (门禁: MFFF ≥ 2.0)
  ≥ 1.5: 中强正向动量 (门禁: MFFF ≥ 1.5)
  ≥ 1.0: 正向动量 (门禁: MFFF ≥ 1.0)
  ≥ 0.5: 弱正向动量
  ≈ 0:   无动量
  < 0:   负向动量

对应门禁策略族群:
  5min 高动量:    MFFF ≥ 2.0
  15min 超卖反转: MFFF ≥ 1.0
  日线深度反转:   MFFF ≥ 1.5

参数空间:
  macd_fast: [8, 12]
  macd_slow: [26]
  macd_signal: [9]
  volume_weight: [0.3, 0.5, 0.7]
  threshold: [0.5, 1.0, 1.5, 2.0]
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.factors.base import BaseFactor, FactorParams, FactorTimeframe, FactorValue

logger = logging.getLogger(__name__)


class MFFFFactor(BaseFactor):
    """
    MomentumFlowFilterFactor — 动量过滤因子
    
    计算逻辑:
    1. MACD 信号: (MACD - Signal) / price → 归一化动量
    2. 资金流: 价格变化 × 成交量 → OBV 斜率近似
    3. 成交量确认: 当前量 / 历史量 → 动量是否有量支撑
    
    综合: score = w_macd * macd_signal + w_flow * flow_signal + w_vol * vol_confirm
    """

    name = "MFFF"
    timeframe = FactorTimeframe.MIN_15
    version = "1.0"

    param_space: Dict[str, List[Any]] = {
        "macd_fast": [8, 12],
        "macd_slow": [26],
        "macd_signal": [9],
        "volume_weight": [0.3, 0.5, 0.7],
    }

    def compute(
        self,
        symbol: str,
        market_data: Dict[str, Any],
        ctx: Optional[Dict[str, Any]] = None,
    ) -> FactorValue:
        bars = market_data.get("bars", [])
        macd_fast = self.params.get("macd_fast", 12)
        macd_slow = self.params.get("macd_slow", 26)
        macd_signal_period = self.params.get("macd_signal", 9)
        vol_weight = self.params.get("volume_weight", 0.5)

        min_bars = macd_slow + macd_signal_period + 5
        if len(bars) < min_bars:
            return FactorValue(
                name=self.name, value=0.0, score=0.0,
                direction_hint="NEUTRAL", confidence=0.0,
                metadata={"error": "insufficient_bars", "need": min_bars, "have": len(bars)},
                params_used=self.params.params,
            )

        closes = [b["close"] for b in bars]
        volumes = [b.get("volume", 0) for b in bars]

        # ── 1. MACD 信号 ──
        macd_line, signal_line, histogram = self._compute_macd(
            closes, macd_fast, macd_slow, macd_signal_period
        )

        # 归一化 MACD histogram: 除以当前价格
        current_price = closes[-1]
        macd_normalized = self._safe_divide(histogram, current_price) * 1000
        # 典型范围: [-5, +5] → 映射到 [-1, 1]
        macd_signal = self._clamp(macd_normalized / 5.0, -1.0, 1.0)

        # ── 2. 资金流 (OBV 斜率近似) ──
        flow_signal = self._compute_flow_signal(closes, volumes, lookback=10)
        # flow_signal ∈ [-1, 1]

        # ── 3. 成交量确认 ──
        vol_ratio = self._compute_volume_confirmation(volumes, lookback=20)
        # vol_ratio: 1.0 = 正常, >1.5 = 放量, <0.5 = 缩量
        vol_confirm = self._clamp((vol_ratio - 1.0), -0.5, 1.0)

        # ── 综合评分 ──
        # MACD 权重 (1 - vol_weight) 分配给 macd 和 flow
        macd_w = (1.0 - vol_weight) * 0.6
        flow_w = (1.0 - vol_weight) * 0.4
        vol_w = vol_weight

        raw_score = macd_w * macd_signal + flow_w * flow_signal
        # 成交量作为确认: 同向放量增强，反向缩量减弱
        vol_multiplier = 1.0 + vol_confirm * (1.0 if raw_score > 0 else -1.0)
        raw_score *= max(vol_multiplier, 0.5)

        # 映射到 [-2, 2] 范围 (与门禁策略的 MFFF 值对齐)
        final_score = self._clamp(raw_score * 2.0, -2.0, 2.0)

        # 方向判断
        if final_score >= 0.3:
            direction = "LONG"
        elif final_score <= -0.3:
            direction = "SHORT"
        else:
            direction = "NEUTRAL"

        # 置信度
        signals_agree = sum(1 for s in [macd_signal, flow_signal]
                          if (s > 0) == (raw_score > 0) and abs(s) > 0.2)
        confidence = self._clamp(0.4 + 0.2 * signals_agree + 0.1 * (vol_confirm > 0.2), 0.0, 1.0)

        return FactorValue(
            name=self.name,
            value=final_score,
            score=self._clamp(final_score / 2.0, -1.0, 1.0),  # 归一化到 [-1, 1]
            direction_hint=direction,
            confidence=confidence,
            metadata={
                "macd_line": round(macd_line, 6),
                "signal_line": round(signal_line, 6),
                "histogram": round(histogram, 6),
                "macd_normalized": round(macd_normalized, 4),
                "macd_signal": round(macd_signal, 4),
                "flow_signal": round(flow_signal, 4),
                "vol_ratio": round(vol_ratio, 2),
                "vol_confirm": round(vol_confirm, 4),
                "raw_score": round(raw_score, 4),
            },
            params_used=self.params.params,
        )

    # ── 技术指标计算 ──

    @staticmethod
    def _ema(values: List[float], period: int) -> List[float]:
        """计算 EMA 序列"""
        if len(values) < period:
            return values[:]

        multiplier = 2.0 / (period + 1)
        ema = [sum(values[:period]) / period]  # 首个值 = SMA

        for val in values[period:]:
            ema.append((val - ema[-1]) * multiplier + ema[-1])

        return ema

    def _compute_macd(
        self,
        closes: List[float],
        fast: int,
        slow: int,
        signal_period: int,
    ) -> tuple:
        """
        计算 MACD
        
        Returns:
            (macd_line, signal_line, histogram) — 最新值
        """
        ema_fast = self._ema(closes, fast)
        ema_slow = self._ema(closes, slow)

        # 对齐长度 (EMA slow 较短)
        offset = len(ema_fast) - len(ema_slow)
        macd_values = [
            ema_fast[offset + i] - ema_slow[i]
            for i in range(len(ema_slow))
        ]

        if len(macd_values) < signal_period:
            macd_line = macd_values[-1] if macd_values else 0.0
            return macd_line, 0.0, macd_line

        signal_values = self._ema(macd_values, signal_period)
        # 对齐
        sig_offset = len(macd_values) - len(signal_values)

        macd_line = macd_values[-1]
        signal_line = signal_values[-1]
        histogram = macd_line - signal_line

        return macd_line, signal_line, histogram

    @staticmethod
    def _compute_flow_signal(
        closes: List[float], volumes: List[float], lookback: int
    ) -> float:
        """
        资金流信号 — 简化版 OBV 斜率
        
        正 = 资金流入, 负 = 资金流出
        返回 [-1, 1]
        """
        if len(closes) < lookback + 1 or len(volumes) < lookback + 1:
            return 0.0

        obv_changes = []
        for i in range(-lookback, 0):
            price_change = closes[i] - closes[i - 1]
            if price_change > 0:
                obv_changes.append(volumes[i])
            elif price_change < 0:
                obv_changes.append(-volumes[i])
            else:
                obv_changes.append(0)

        net_flow = sum(obv_changes)
        total_vol = sum(abs(v) for v in obv_changes)

        if total_vol == 0:
            return 0.0

        # 归一化到 [-1, 1]
        return max(-1.0, min(1.0, net_flow / total_vol))

    @staticmethod
    def _compute_volume_confirmation(volumes: List[float], lookback: int) -> float:
        """成交量确认 — 当前量 / 历史均值"""
        if len(volumes) < lookback + 1:
            return 1.0

        current = volumes[-1]
        historical = volumes[-(lookback + 1):-1]
        avg = sum(historical) / len(historical) if historical else 1.0

        if avg == 0:
            return 1.0

        return current / avg
