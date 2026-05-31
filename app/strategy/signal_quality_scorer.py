"""多因子信号质量评分器 (Signal Quality Scorer)

Phase 1 (2026-05-31): 替换硬编码置信度，用 5 个独立维度动态计算信号质量
每个维度 0.0~1.0，加权求和 + 汇合奖励/矛盾惩罚 → final confidence

维度:
  D1: 趋势质量 (MA排列 + 斜率 + SPY共振) weight=0.25
  D2: 动量 (RSI位置 + MACD柱 + slope) weight=0.20
  D3: 成交量 (volume_ratio + VWAP + 量价一致) weight=0.25
  D4: 资金流 (order_flow + ticker + 大单) weight=0.15
  D5: 市场环境 (VIX + SPY/MA200 + regime) weight=0.15
"""
import logging
from dataclasses import dataclass, field
from typing import Dict, Any

logger = logging.getLogger(__name__)


@dataclass
class QualityResult:
    """信号质量评分结果"""
    confidence: float = 0.5       # 最终置信度 0.0~1.0
    strength: int = 50            # 信号强度 0~99
    dimensions: Dict[str, float] = field(default_factory=dict)  # 各维度得分
    confluence_bonus: float = 0.0       # 汇合奖励
    contradiction_penalty: float = 0.0  # 矛盾惩罚
    detail: Dict[str, Any] = field(default_factory=dict)  # 详细计算过程

    def to_dict(self) -> dict:
        return {
            "confidence": round(self.confidence, 3),
            "strength": self.strength,
            "dimensions": {k: round(v, 3) for k, v in self.dimensions.items()},
            "confluence_bonus": round(self.confluence_bonus, 3),
            "contradiction_penalty": round(self.contradiction_penalty, 3),
        }


class SignalQualityScorer:
    """多因子信号质量评分器"""

    # 维度权重
    WEIGHTS = {
        "trend": 0.25,
        "momentum": 0.20,
        "volume": 0.25,
        "order_flow": 0.15,
        "market": 0.15,
    }

    # 汇合奖励表: dimensions_agreeing → bonus
    CONFLUENCE_BONUS = {0: -0.10, 1: -0.05, 2: 0.0, 3: +0.05, 4: +0.10, 5: +0.15}

    @classmethod
    def score(cls, ctx, direction: str) -> QualityResult:
        """对 SymbolContext + 方向 计算信号质量

        Args:
            ctx: SymbolContext 对象 (app/core/pipeline_contracts.py)
            direction: "LONG" or "SHORT"

        Returns:
            QualityResult with confidence, strength, and dimension breakdowns
        """
        is_long = direction.upper() in ("LONG", "BUY")

        # ── D1: 趋势质量 ──
        d_trend = cls._score_trend(ctx, is_long)

        # ── D2: 动量 ──
        d_momentum = cls._score_momentum(ctx, is_long)

        # ── D3: 成交量 ──
        d_volume = cls._score_volume(ctx, is_long)

        # ── D4: 资金流 ──
        d_order_flow = cls._score_order_flow(ctx, is_long)

        # ── D5: 市场环境 ──
        d_market = cls._score_market(ctx, is_long)

        dimensions = {
            "trend": d_trend,
            "momentum": d_momentum,
            "volume": d_volume,
            "order_flow": d_order_flow,
            "market": d_market,
        }

        # 加权求和
        weighted_sum = sum(
            dimensions[k] * cls.WEIGHTS[k] for k in cls.WEIGHTS
        )

        # 汇合奖励: 多少维度 > 0.6
        agreeing = sum(1 for d in dimensions.values() if d > 0.6)
        confluence_bonus = cls.CONFLUENCE_BONUS.get(agreeing, 0.0)

        # 矛盾惩罚: 多少维度 < 0.3
        contradicting = sum(1 for d in dimensions.values() if d < 0.3)
        contradiction_penalty = -0.10 * contradicting

        raw_confidence = weighted_sum + confluence_bonus + contradiction_penalty
        confidence = max(0.10, min(0.95, raw_confidence))

        # Strength: 从 confidence 映射到 0-99，考虑汇合程度
        base_strength = int(confidence * 80) + agreeing * 3
        strength = max(10, min(99, base_strength))

        result = QualityResult(
            confidence=round(confidence, 3),
            strength=strength,
            dimensions=dimensions,
            confluence_bonus=confluence_bonus,
            contradiction_penalty=contradiction_penalty,
            detail={
                "agreeing_dims": agreeing,
                "contradicting_dims": contradicting,
                "weighted_sum": round(weighted_sum, 3),
                "direction": direction,
            },
        )

        logger.info(
            f"[SignalQuality] {getattr(ctx, 'symbol', '?')} {direction} "
            f"conf={confidence:.3f} str={strength} "
            f"dims={{{', '.join(f'{k}={v:.2f}' for k, v in dimensions.items())}}} "
            f"confluence={confluence_bonus:+.2f} penalty={contradiction_penalty:+.2f}"
        )

        return result

    # ─────────────────────────────────────────────────────────
    #  D1: 趋势质量 (weight 0.25)
    # ─────────────────────────────────────────────────────────
    @classmethod
    def _score_trend(cls, ctx, is_long: bool) -> float:
        score = 0.0
        price = getattr(ctx, "price", 0) or 0
        ma5 = getattr(ctx, "ma5", 0) or 0
        ma20 = getattr(ctx, "ma20", 0) or 0
        ma50 = getattr(ctx, "ma50", 0) or 0

        # MA 排列 (完美排列 0.4, 部分排列 0.2)
        if all(v > 0 for v in [price, ma5, ma20, ma50]):
            if is_long:
                if price > ma5 > ma20 > ma50:
                    score += 0.4  # 完美多头排列
                elif price > ma20 > ma50:
                    score += 0.25
                elif price > ma20:
                    score += 0.15
            else:  # SHORT
                if price < ma5 < ma20 < ma50:
                    score += 0.4  # 完美空头排列
                elif price < ma20 < ma50:
                    score += 0.25
                elif price < ma20:
                    score += 0.15

        # MA 斜率 (MA20 方向)
        trend = getattr(ctx, "trend", "") or ""
        if is_long and trend.upper() in ("UP", "BULLISH", "TREND_UP"):
            score += 0.3
        elif not is_long and trend.upper() in ("DOWN", "BEARISH", "TREND_DOWN"):
            score += 0.3
        elif ma20 > 0 and ma50 > 0:
            # Infer: MA20 > MA50 = uptrend
            if is_long and ma20 > ma50:
                score += 0.2
            elif not is_long and ma20 < ma50:
                score += 0.2

        # SPY 共振
        spy_price = getattr(ctx, "spy_price", 0) or 0
        spy_ma200 = getattr(ctx, "spy_ma200", 0) or 0
        if spy_price > 0 and spy_ma200 > 0:
            spy_bullish = spy_price > spy_ma200
            if (is_long and spy_bullish) or (not is_long and not spy_bullish):
                score += 0.3
            else:
                score += 0.1  # partial credit for not being directly against

        return min(1.0, score)

    # ─────────────────────────────────────────────────────────
    #  D2: 动量 (weight 0.20)
    # ─────────────────────────────────────────────────────────
    @classmethod
    def _score_momentum(cls, ctx, is_long: bool) -> float:
        score = 0.0
        rsi = getattr(ctx, "rsi", None)
        macd = getattr(ctx, "macd", None)
        macd_signal = getattr(ctx, "macd_signal", None)
        slope_5m = getattr(ctx, "slope_5m", None)

        # RSI 位置评分 (pullback 区间内 = 高分)
        if rsi is not None and rsi > 0:
            if is_long:
                if 35 <= rsi <= 50:  # 完美回调区
                    score += 0.5
                elif 30 <= rsi <= 55:  # 可接受区
                    score += 0.3
                elif 55 < rsi <= 65:  # 偏高但仍可
                    score += 0.15
                elif rsi > 70:  # 超买，逆向风险
                    score += 0.0
            else:  # SHORT
                if 50 <= rsi <= 65:  # 完美反弹区
                    score += 0.5
                elif 45 <= rsi <= 70:
                    score += 0.3
                elif 35 <= rsi < 45:
                    score += 0.15
                elif rsi < 30:  # 超卖
                    score += 0.0

        # MACD 柱方向
        if macd is not None and macd_signal is not None:
            histogram = macd - macd_signal
            if is_long and histogram > 0:
                score += 0.3
            elif not is_long and histogram < 0:
                score += 0.3
            elif is_long and histogram > -0.005:  # nearly flat, acceptable
                score += 0.1
            elif not is_long and histogram < 0.005:
                score += 0.1

        # slope_5m (短期动量)
        if slope_5m is not None:
            if is_long and slope_5m > 0:
                score += 0.2
            elif not is_long and slope_5m < 0:
                score += 0.2
            elif is_long and slope_5m > -0.5:  # slight dip, OK for pullback
                score += 0.1
            elif not is_long and slope_5m < 0.5:
                score += 0.1

        return min(1.0, score)

    # ─────────────────────────────────────────────────────────
    #  D3: 成交量 (weight 0.25)
    # ─────────────────────────────────────────────────────────
    @classmethod
    def _score_volume(cls, ctx, is_long: bool) -> float:
        score = 0.0
        volume_ratio = getattr(ctx, "volume_ratio", None)
        volume = getattr(ctx, "volume", 0) or 0
        avg_volume = getattr(ctx, "avg_volume", 0) or 0

        # Volume ratio
        if volume_ratio is not None and volume_ratio > 0:
            if volume_ratio >= 2.0:
                score += 0.6  # 放量明显
            elif volume_ratio >= 1.5:
                score += 0.45
            elif volume_ratio >= 1.2:
                score += 0.35
            elif volume_ratio >= 0.8:
                score += 0.2  # 正常
            else:
                score += 0.05  # 缩量，不利

        # VWAP 偏离 (如果有)
        vwap_deviation = getattr(ctx, "vwap_deviation", None)
        if vwap_deviation is None:
            # Try metadata
            metadata = getattr(ctx, "metadata", {}) or {}
            vwap_deviation = metadata.get("vwap_deviation")

        if vwap_deviation is not None:
            try:
                vd = float(vwap_deviation)
                if is_long and vd > 0:  # 价格在 VWAP 上方
                    score += 0.3
                elif not is_long and vd < 0:  # 价格在 VWAP 下方
                    score += 0.3
                elif abs(vd) < 0.005:  # 接近 VWAP
                    score += 0.15
            except (TypeError, ValueError):
                pass

        # 量价一致性 (简化: volume > avg_volume 且方向一致)
        if volume > 0 and avg_volume > 0:
            vol_above_avg = volume > avg_volume
            price = getattr(ctx, "price", 0) or 0
            ma20 = getattr(ctx, "ma20", 0) or 0
            if price > 0 and ma20 > 0:
                if is_long:
                    if price >= ma20 and vol_above_avg:
                        score += 0.3  # 趋势确认放量
                    elif price < ma20 and not vol_above_avg:
                        score += 0.25  # 健康回调缩量
                    else:
                        score += 0.1
                else:  # SHORT
                    if price <= ma20 and vol_above_avg:
                        score += 0.3
                    elif price > ma20 and not vol_above_avg:
                        score += 0.25
                    else:
                        score += 0.1

        return min(1.0, score)

    # ─────────────────────────────────────────────────────────
    #  D4: 资金流 (weight 0.15)
    # ─────────────────────────────────────────────────────────
    @classmethod
    def _score_order_flow(cls, ctx, is_long: bool) -> float:
        score = 0.0

        # Order flow imbalance (bid_vol vs ask_vol)
        ofi = getattr(ctx, "order_flow_imbalance", None)
        if ofi is not None:
            try:
                ofi_f = float(ofi)
                if is_long:
                    if ofi_f > 0.3:
                        score += 0.5  # 买方主导
                    elif ofi_f > 0.1:
                        score += 0.35
                    elif ofi_f > 0:
                        score += 0.2
                    elif ofi_f > -0.2:
                        score += 0.1  # 轻微卖方
                    # else: 0 — strong selling, no score for LONG
                else:  # SHORT
                    if ofi_f < -0.3:
                        score += 0.5  # 卖方主导
                    elif ofi_f < -0.1:
                        score += 0.35
                    elif ofi_f < 0:
                        score += 0.2
                    elif ofi_f < 0.2:
                        score += 0.1
            except (TypeError, ValueError):
                pass

        # Ticker buy/sell ratio
        bsr = getattr(ctx, "ticker_buy_sell_ratio", None)
        if bsr is not None:
            try:
                bsr_f = float(bsr)
                if is_long and bsr_f > 1.1:
                    score += 0.3
                elif is_long and bsr_f > 1.0:
                    score += 0.15
                elif not is_long and bsr_f < 0.9:
                    score += 0.3
                elif not is_long and bsr_f < 1.0:
                    score += 0.15
            except (TypeError, ValueError):
                pass

        # Large order ratio (from metadata if available)
        metadata = getattr(ctx, "metadata", {}) or {}
        large_order = metadata.get("large_order_ratio")
        if large_order is not None:
            try:
                lo = float(large_order)
                if lo > 0.5:  # 大单偏多
                    score += 0.2
                elif lo > 0.3:
                    score += 0.1
            except (TypeError, ValueError):
                pass

        # If no order flow data available, return neutral
        if ofi is None and bsr is None and large_order is None:
            return 0.5  # neutral, don't penalize for missing data

        return min(1.0, score)

    # ─────────────────────────────────────────────────────────
    #  D5: 市场环境 (weight 0.15)
    # ─────────────────────────────────────────────────────────
    @classmethod
    def _score_market(cls, ctx, is_long: bool) -> float:
        score = 0.0

        # VIX regime
        vix = getattr(ctx, "vix", None)
        vix_regime = getattr(ctx, "vix_regime", None) or ""

        if vix is not None:
            try:
                vix_f = float(vix)
                if vix_f <= 20:
                    score += 0.4  # 平静市场
                elif vix_f <= 30:
                    score += 0.25  # 正常波动
                elif vix_f <= 40:
                    score += 0.15  # 高波动
                else:
                    score += 0.05  # 极端
            except (TypeError, ValueError):
                score += 0.2  # default
        elif vix_regime:
            regime_lower = vix_regime.lower()
            if "normal" in regime_lower or "low" in regime_lower:
                score += 0.4
            elif "high" in regime_lower or "elevated" in regime_lower:
                score += 0.2
            elif "extreme" in regime_lower:
                score += 0.05
            else:
                score += 0.2

        # SPY vs MA200
        spy_price = getattr(ctx, "spy_price", 0) or 0
        spy_ma200 = getattr(ctx, "spy_ma200", 0) or 0
        if spy_price > 0 and spy_ma200 > 0:
            spy_bull = spy_price > spy_ma200
            if is_long and spy_bull:
                score += 0.3
            elif not is_long and not spy_bull:
                score += 0.3
            else:
                score += 0.1  # counter-trend but not zero

        # Market regime (trend-friendly vs mean-reversion)
        regime = getattr(ctx, "market_regime", "") or ""
        regime_lower = regime.lower()
        if "trend" in regime_lower:
            score += 0.3  # trending = good for directional signals
        elif "range" in regime_lower or "sideways" in regime_lower:
            score += 0.15  # less favorable for directional
        else:
            score += 0.2

        return min(1.0, score)
