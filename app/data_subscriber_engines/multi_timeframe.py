"""
Multi-Timeframe (MTF) Trend Engine

Extracted from DataSubscriber (2026-05-02 Harness Pilot Phase).
Pure calculation logic — no side effects, no DataSubscriber dependency.

Original location: app/data_subscriber.py L783-1184 (~400 lines)

Usage:
    from app.data_subscriber.multi_timeframe import (
        MultiTimeframeEngine,
        MTFConfig
    )

    engine = MultiTimeframeEngine()
    engine.update_trend(code, k_type, kline, kline_buffers, trends)
    score, detail = engine.get_alignment_score(code, action, trends)
    confirmed, boost, reason = engine.get_signal(code, action, trends)
"""

import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "MultiTimeframeEngine",
    "MTFConfig",
    "DEFAULT_TREND_STATE",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_TREND_STATE = {
    "K_1M": "neutral",
    "K_3M": "neutral",
    "K_5M": "neutral",
    "K_15M": "neutral",
    "K_30M": "neutral",
    "K_60M": "neutral",
    "K_DAY": "neutral",
}

# Minimum data length required per kline period
MIN_LEN_MAP = {
    "K_1M": 15,
    "K_3M": 15,
    "K_5M": 18,
    "K_15M": 25,
    "K_30M": 38,
    "K_60M": 38,
    "K_DAY": 55,
}

# How long each period takes to accumulate minimum data after cold start (seconds)
PERIOD_COLD_START_SECONDS = {
    "K_1M":  15 * 60,       # ~15 min
    "K_3M":  15 * 180,      # ~45 min
    "K_5M":  18 * 300,      # ~90 min
    "K_15M": 25 * 900,      # ~6.25 hours (~1 trading day)
    "K_30M": 38 * 1800,     # ~19 hours (~3 trading days)
    "K_60M": 38 * 3600,     # ~38 hours (~6 trading days)
    "K_DAY": 55 * 86400,    # ~55 days
}

# Alignment score weights (sum = 1.0)
PERIOD_WEIGHTS_ALIGNMENT = {
    "K_DAY": 0.30,
    "K_60M": 0.20,
    "K_30M": 0.15,
    "K_15M": 0.12,
    "K_5M":  0.10,
    "K_3M":  0.08,
    "K_1M":  0.05,
}

# Resonance score weights (sum = 1.0)
RESONANCE_WEIGHTS = {
    "K_DAY": 0.35,
    "K_60M": 0.25,
    "K_30M": 0.15,
    "K_15M": 0.10,
    "K_5M":  0.07,
    "K_3M":  0.05,
    "K_1M":  0.03,
}

# MTF confidence boost tiers
MTF_BOOST_TIERS = {
    "strong_resonance": 0.20,    # score >= 0.70
    "medium_resonance": 0.12,    # score >= 0.55
    "weak_resonance":   0.05,    # score >= 0.40
    "neutral":          0.0,     # score >= 0.20
    "conflict":        -0.08,    # score < 0.20
}

# Trend string to Trend enum mapping (for trend_filter sync)
TREND_TO_ENUM = {
    "up": "WEAK_UP",
    "down": "WEAK_DOWN",
    "neutral": "RANGE",
}

# Periods that need trend_filter sync
TREND_FILTER_SYNC = {
    "K_DAY": ("update_daily_trend", "consensus"),
    "K_60M": ("update_k60m_trend", "trend_filter"),
    "K_3M": ("update_k3m_trend", "trend_filter"),
    "K_5M": ("update_k5m_trend", "trend_filter"),
}


# MTF warmup grace period after cold start (seconds)
MTF_WARMUP_SECONDS = 30 * 60  # 30 minutes — skip MTF rejection during this window

# Minimum alignment threshold (absolute floor)
MTF_MIN_ALIGNMENT_THRESHOLD = 0.35

# Default alignment threshold when all 7 periods available
MTF_DEFAULT_THRESHOLD = 0.35


class MTFConfig:
    """MTF Engine configuration."""

    def __init__(
        self,
        mtf_weights: Optional[Dict[str, float]] = None,
        mtf_boost: Optional[Dict[str, float]] = None,
    ):
        self.mtf_weights = mtf_weights or {}
        self.mtf_boost = mtf_boost or {}


class MultiTimeframeEngine:
    """
    Multi-Timeframe Trend Engine.

    Pure calculation logic — no side effects on DataSubscriber state.
    All state mutations happen through explicit return values and callbacks.
    """

    def __init__(self, config: Optional[MTFConfig] = None):
        self.config = config or MTFConfig()
        self._logger = logging.getLogger(f"{__name__}.MTFEngine")

    def update_trend(
        self,
        code: str,
        k_type: str,
        kline: dict,
        kline_buffers: Dict[str, Dict[str, Any]],
        trends: Dict[str, Dict[str, str]],
        consensus=None,
        trend_filter=None,
    ) -> Optional[str]:
        """
        Update multi-timeframe trend state for a code/period.

        Returns the new trend ("up"/"down"/"neutral") or None if data
        insufficient. Does NOT mutate DataSubscriber directly — the caller
        is responsible for persisting the result.

        Args:
            code:       Stock code (e.g. "HK.00700")
            k_type:     Kline period (K_1M, K_3M, K_5M, K_15M, K_30M, K_60M, K_DAY)
            kline:      New kline dict with open/high/low/close/volume
            kline_buffers:  {code: {period: IndicatorBuffer}}
            trends:     {code: {period: "up"/"down"/"neutral"}}
            consensus:  Optional ConsensusEngine for daily trend sync
            trend_filter: Optional TrendFilter for K_60M/K_3M/K_5M sync
        """
        from app.strategy.indicators import rsi as calc_rsi

        try:
            buf = kline_buffers[code][k_type]
            buf.push_kline(kline)

            closes = list(buf.close)
            min_len = MIN_LEN_MAP.get(k_type, 20)
            if len(closes) < min_len:
                return None

            current_price = closes[-1]

            # Compute MA based on period
            ma_fast, ma_slow, ma20, ma50 = self._compute_ma(closes, k_type)

            # Compute RSI(14)
            rsi_val = calc_rsi(closes, period=14)

            # Determine trend direction
            trend = self._judge_trend(k_type, current_price, ma_fast, ma_slow,
                                       ma20, ma50, rsi_val)

            # Caller persists the trend — we only return it
            return trend

        except Exception as e:
            self._logger.debug(f"[MTF] {code} {k_type} update failed: {e}")
            return None

    def get_alignment_score(
        self,
        code: str,
        action: str,
        trends: Dict[str, Dict[str, str]],
        updated_periods: Optional[set] = None,
    ) -> Tuple[float, str]:
        """
        Calculate multi-timeframe alignment score (0.0 ~ 1.0).

        When updated_periods is provided, only periods that have received at
        least one successful trend update are included in scoring — periods
        still at their default "neutral" are excluded and weights renormalized.
        This prevents cold-start rejection where all periods are neutral
        simply because K-line buffers haven't filled yet.

        Returns: (score, detail_string)
        """
        weights = self._get_period_weights()
        code_trends = trends.get(code, DEFAULT_TREND_STATE.copy())
        is_buy = (action == "BUY")
        target_trend = "up" if is_buy else "down"
        opposite_trend = "down" if is_buy else "up"

        # Determine which periods to include in scoring
        if updated_periods is not None:
            active_periods = [p for p in weights if p in updated_periods]
            if not active_periods:
                # No periods have been updated yet — cold start, return 0.50
                # (neutral pass-through, let warmup grace period handle rejection)
                return 0.50, "冷启动(无可用周期)"
            # Renormalize weights to sum to 1.0 over available periods
            active_weight_sum = sum(weights[p] for p in active_periods)
            effective_weights = {p: weights[p] / active_weight_sum for p in active_periods}
        else:
            effective_weights = weights
            active_periods = list(weights.keys())

        aligned_weight = 0.0
        conflict_weight = 0.0
        detail_parts = []

        for period in weights:
            w = effective_weights.get(period, 0.0)
            t = code_trends.get(period, "neutral")
            if w == 0.0:
                detail_parts.append(f"{period}·")  # excluded (not updated yet)
            elif t == target_trend:
                aligned_weight += w
                detail_parts.append(f"{period}↑")
            elif t == opposite_trend:
                conflict_weight += w
                detail_parts.append(f"{period}↓")
            else:
                detail_parts.append(f"{period}~")

        raw_score = aligned_weight - conflict_weight * 0.5
        mtf_score = max(0.0, min(1.0, raw_score))
        detail = " | ".join(detail_parts)
        return mtf_score, detail

    def get_signal(
        self,
        code: str,
        action: str,
        trends: Dict[str, Dict[str, str]],
        consensus=None,
        updated_periods: Optional[set] = None,
    ) -> Tuple[bool, float, str]:
        """
        Get multi-timeframe trend confirmation signal.

        Returns: (confirmed, confidence_boost, reason)
        """
        code_trends = trends.get(code, DEFAULT_TREND_STATE.copy())

        day_trend = code_trends.get("K_DAY", "neutral")
        h1_trend = code_trends.get("K_60M", "neutral")
        m5_trend = code_trends.get("K_5M", "neutral")
        m3_trend = code_trends.get("K_3M", "neutral")

        is_buy = action == "BUY"

        # Day + 60M dual filter (both opposite → reject)
        # Only apply if both periods have been updated (not default neutral)
        if updated_periods is not None:
            day_updated = "K_DAY" in updated_periods
            h1_updated = "K_60M" in updated_periods
        else:
            day_updated = h1_updated = True

        if day_updated and h1_updated and day_trend != "neutral" and h1_trend != "neutral":
            both_against = (
                is_buy and day_trend == "down" and h1_trend == "down"
            ) or (
                not is_buy and day_trend == "up" and h1_trend == "up"
            )
            if both_against:
                return False, 0.0, f"日线+60M双重逆势（日={day_trend}, 60M={h1_trend}）"

        # Ultra-short (3M + 5M) dual reverse intercept
        short_both_against = (
            is_buy and m5_trend == "down" and m3_trend == "down"
        ) or (
            not is_buy and m5_trend == "up" and m3_trend == "up"
        )
        if short_both_against:
            return False, 0.0, f"超短线3M+5M双重逆势（3M={m3_trend}, 5M={m5_trend}）"

        # Single day reverse → penalty but not reject (only if day was updated)
        single_against = False
        if day_updated and day_trend != "neutral":
            single_against = (
                (is_buy and day_trend == "down") or
                (not is_buy and day_trend == "up")
            )

        # Weighted alignment score
        mtf_score, detail = self.get_alignment_score(code, action, trends, updated_periods)

        # Resonance score
        resonance_score = self.calculate_resonance(code, action, trends)

        # Tiered confidence boost
        boost = self._get_boost(mtf_score, single_against)

        up_count = sum(1 for t in code_trends.values() if t == "up")
        down_count = sum(1 for t in code_trends.values() if t == "down")
        reason = (
            f"MTF对齐={mtf_score:.2f}({detail}) "
            f"共振度={resonance_score:.2f} "
            f"上{up_count}/下{down_count}/7"
        )

        # Inject resonance into consensus if available
        if consensus:
            try:
                if action == "BUY":
                    consensus.update_mtf_alignment(code, resonance_score, 1.0 - resonance_score)
                else:
                    consensus.update_mtf_alignment(code, 1.0 - resonance_score, resonance_score)
            except Exception:
                pass  # Non-critical

        return True, round(boost, 3), reason

    def calculate_resonance(
        self,
        code: str,
        action: str,
        trends: Dict[str, Dict[str, str]],
    ) -> float:
        """Calculate multi-timeframe resonance (0.0 ~ 1.0)."""
        code_trends = trends.get(code, DEFAULT_TREND_STATE.copy())
        target_trend = "up" if action == "BUY" else "down"

        aligned_weight = sum(
            w for k, w in RESONANCE_WEIGHTS.items()
            if code_trends.get(k, "neutral") == target_trend
        )
        total_weight = sum(
            w for k, w in RESONANCE_WEIGHTS.items()
            if code_trends.get(k, "neutral") != "neutral"
        )

        if total_weight == 0:
            return 0.5
        return round(aligned_weight / total_weight, 3)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_ma(closes, k_type):
        """Compute moving averages for a given period."""
        ma_fast = ma_slow = ma20 = ma50 = None

        if k_type in ("K_1M", "K_3M"):
            ma_fast = sum(closes[-5:]) / 5
            ma_slow = sum(closes[-10:]) / 10
        elif k_type == "K_5M":
            ma_fast = sum(closes[-8:]) / 8
            ma_slow = sum(closes[-13:]) / 13
        elif k_type == "K_15M":
            ma_fast = sum(closes[-8:]) / 8
            ma_slow = sum(closes[-21:]) / 21
        elif k_type in ("K_30M", "K_60M"):
            ma_fast = sum(closes[-13:]) / 13
            ma_slow = sum(closes[-34:]) / 34
        elif k_type == "K_DAY":
            ma20 = sum(closes[-20:]) / 20
            ma50 = sum(closes[-50:]) / 50
            ma_fast = ma20
            ma_slow = ma50

        return ma_fast, ma_slow, ma20, ma50

    @staticmethod
    def _judge_trend(k_type, current_price, ma_fast, ma_slow, ma20, ma50, rsi_val):
        """Judge trend direction based on MA + RSI."""
        if ma_fast is None or ma_slow is None:
            return "neutral"

        if k_type in ("K_1M",):
            rsi_up = (rsi_val or 50) > 52
            rsi_down = (rsi_val or 50) < 48
        elif k_type == "K_3M":
            rsi_up = (rsi_val or 50) > 51
            rsi_down = (rsi_val or 50) < 49
        elif k_type == "K_5M":
            rsi_up = (rsi_val or 50) > 51
            rsi_down = (rsi_val or 50) < 49
        elif k_type == "K_15M":
            rsi_up = (rsi_val or 50) > 50
            rsi_down = (rsi_val or 50) < 50
        elif k_type in ("K_30M", "K_60M"):
            rsi_up = (rsi_val or 50) > 48
            rsi_down = (rsi_val or 50) < 52
        elif k_type == "K_DAY":
            rsi_up = (rsi_val or 50) > 45
            rsi_down = (rsi_val or 50) < 55
        else:
            rsi_up = (rsi_val or 50) > 50
            rsi_down = (rsi_val or 50) < 50

        # Day special case (MA20/MA50 dual filter)
        if k_type == "K_DAY":
            if current_price > ma20 and ma20 > ma50 and rsi_up:
                return "up"
            elif current_price < ma20 and ma20 < ma50 and rsi_down:
                return "down"
            elif current_price > ma20:
                return "up" if rsi_up else "neutral"
            elif current_price < ma20:
                return "down" if rsi_down else "neutral"
            else:
                return "neutral"

        # General case
        if ma_fast > ma_slow and current_price > ma_fast and rsi_up:
            return "up"
        elif ma_fast < ma_slow and current_price < ma_fast and rsi_down:
            return "down"
        else:
            return "neutral"

    def _get_period_weights(self):
        """Get alignment weights, merged with config overrides."""
        w = dict(PERIOD_WEIGHTS_ALIGNMENT)
        w.update(self.config.mtf_weights)
        return w

    def _get_boost(self, mtf_score, single_against):
        """Get confidence boost based on alignment score tier."""
        boost_cfg = self.config.mtf_boost or {}
        if single_against:
            return boost_cfg.get("conflict", MTF_BOOST_TIERS["conflict"]) + mtf_score * 0.05
        elif mtf_score >= 0.70:
            return boost_cfg.get("strong_resonance", MTF_BOOST_TIERS["strong_resonance"])
        elif mtf_score >= 0.55:
            return boost_cfg.get("medium_resonance", MTF_BOOST_TIERS["medium_resonance"])
        elif mtf_score >= 0.40:
            return boost_cfg.get("weak_resonance", MTF_BOOST_TIERS["weak_resonance"])
        elif mtf_score >= 0.20:
            return boost_cfg.get("neutral", MTF_BOOST_TIERS["neutral"])
        else:
            return boost_cfg.get("conflict", MTF_BOOST_TIERS["conflict"])
