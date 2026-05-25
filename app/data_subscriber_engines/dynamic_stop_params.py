"""
Dynamic Stop-Loss / Take-Profit Parameter Engine

Extracted from DataSubscriber (2026-05-02 Harness Pilot Phase).
Pure calculation logic — no side effects, no DataSubscriber dependency.

Original location: app/data_subscriber.py L1186-1340 (~155 lines)

Usage:
    from app.data_subscriber.dynamic_stop_params import (
        DynamicStopEngine,
        StopConfig
    )

    engine = DynamicStopEngine()
    params = engine.get_params(code, trends, scenario_type=None)
"""

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "DynamicStopEngine",
    "StopConfig",
]


class StopConfig:
    """Configuration for dynamic stop/exit parameters."""

    def __init__(
        self,
        trend_weights: Optional[Dict[str, float]] = None,
        regime_params: Optional[Dict[str, Dict[str, float]]] = None,
        scenario_factors: Optional[Dict[str, Dict[str, float]]] = None,
    ):
        self.trend_weights = trend_weights or self._default_weights()
        self.regime_params = regime_params or self._default_regime_params()
        self.scenario_factors = scenario_factors or self._default_scenario_factors()

    @staticmethod
    def _default_weights():
        return {
            "K_DAY": 0.35, "K_60M": 0.25, "K_30M": 0.15,
            "K_15M": 0.10, "K_5M": 0.07, "K_3M": 0.05, "K_1M": 0.03,
        }

    @staticmethod
    def _default_regime_params():
        return {
            "strong_up": {
                "stop_loss_pct": -0.06, "take_profit_pct": 0.08,
                "breakeven_trigger": 0.025, "trail_atr_mult": 2.8,
            },
            "weak_up": {
                "stop_loss_pct": -0.025, "take_profit_pct": 0.08,  # P1-SSOT: 对齐default,弱上涨不应比default更宽
                "breakeven_trigger": 0.020, "trail_atr_mult": 2.3,
            },
            "strong_down": {
                "stop_loss_pct": -0.04, "take_profit_pct": 0.06,  # P1-SSOT: R:R=1.5:1
                "breakeven_trigger": 0.018, "trail_atr_mult": 1.4,
            },
            "weak_down": {
                "stop_loss_pct": -0.035, "take_profit_pct": 0.06,  # P1-SSOT: R:R=1.71:1
                "breakeven_trigger": 0.020, "trail_atr_mult": 1.7,
            },
            "neutral": {
                "stop_loss_pct": -0.025, "take_profit_pct": 0.08,
                "breakeven_trigger": 0.018, "trail_atr_mult": 1.6,
            },
        }

    @staticmethod
    def _default_scenario_factors():
        """Scenario-aware stop/take-profit adjustment factors."""
        return {
            # Event-driven: tighten stops (high volatility)
            "GAP_EVENT": {"sl": 0.7, "tp": 1.2, "trail": 0.8},
            "NEWS_DRIVEN": {"sl": 0.7, "tp": 1.2, "trail": 0.8},
            # Strong trend: widen stops, raise take-profit (let profits run)
            "TREND_STRONG": {"sl": 1.3, "tp": 1.5, "trail": 1.2},
            # Range-bound: tighten both (quick in quick out)
            "RANGE_BOUND": {"sl": 0.8, "tp": 0.9, "trail": 1.0},
            # Volatility expansion: widen stops (need more tolerance)
            "VOL_EXPANSION": {"sl": 1.2, "tp": 1.1, "trail": 1.1},
            # Trend exhaustion / reversal: tighten stops (high reversal risk)
            "TREND_EXHAUST": {"sl": 0.85, "tp": 1.0, "trail": 0.9},
            "REVERSAL_SETUP": {"sl": 0.85, "tp": 1.0, "trail": 0.9},
        }


class DynamicStopEngine:
    """
    Dynamic Stop-Loss / Take-Profit Parameter Engine.

    Uses multi-timeframe trend state + scenario classification to
    compute appropriate stop/exit parameters.

    Pure calculation — no mutation of DataSubscriber state.
    """

    def __init__(self, config: Optional[StopConfig] = None):
        self.config = config or StopConfig()
        self._logger = logging.getLogger(f"{__name__}.DynamicStopEngine")

    def get_params(
        self,
        code: str,
        trends: Dict[str, Dict[str, str]],
        scenario_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Compute dynamic stop/exit parameters for a stock.

        Args:
            code:           Stock code
            trends:         {code: {period: "up"/"down"/"neutral"}}
            scenario_type:  Optional scenario name (e.g. "TREND_STRONG")

        Returns:
            {
                "stop_loss_pct": float,
                "take_profit_pct": float,
                "breakeven_trigger": float,
                "trail_atr_mult": float,
                "regime": str,
                "scenario_adjustment": str,
            }
        """
        # 1. Seven-period weighted trend score
        code_trends = trends.get(code, {})
        score_map = {"up": 1, "down": -1, "neutral": 0}

        combined = sum(
            score_map.get(code_trends.get(k, "neutral"), 0) * w
            for k, w in self.config.trend_weights.items()
        )

        # 2. Base params from regime
        regime = self._classify_regime(combined)
        base_params = self.config.regime_params[regime]

        # 3. Scenario adjustment
        scenario_adj = self._get_scenario_adjustment(scenario_type)

        # 4. Apply scenario factors
        adjusted = {
            "stop_loss_pct": base_params["stop_loss_pct"] * scenario_adj["sl"],
            "take_profit_pct": base_params["take_profit_pct"] * scenario_adj["tp"],
            "breakeven_trigger": base_params["breakeven_trigger"],
            "trail_atr_mult": base_params["trail_atr_mult"] * scenario_adj["trail"],
            "regime": regime,
            "scenario_adjustment": (
                f"{scenario_type or 'none'} "
                f"(sl={scenario_adj['sl']:.2f}, "
                f"tp={scenario_adj['tp']:.2f})"
            ),
        }

        return adjusted

    def classify_regime(
        self,
        code: str,
        trends: Dict[str, Dict[str, str]],
    ) -> str:
        """
        Classify the trend regime for a stock without computing full params.

        Returns one of: "strong_up", "weak_up", "strong_down", "weak_down", "neutral"
        """
        code_trends = trends.get(code, {})
        score_map = {"up": 1, "down": -1, "neutral": 0}

        combined = sum(
            score_map.get(code_trends.get(k, "neutral"), 0) * w
            for k, w in self.config.trend_weights.items()
        )

        return self._classify_regime(combined)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _classify_regime(combined: float) -> str:
        if combined >= 0.55:
            return "strong_up"
        elif combined >= 0.20:
            return "weak_up"
        elif combined <= -0.55:
            return "strong_down"
        elif combined <= -0.20:
            return "weak_down"
        else:
            return "neutral"

    def _get_scenario_adjustment(self, scenario_type: Optional[str]) -> Dict[str, float]:
        """Get scenario-specific adjustment factors."""
        if not scenario_type:
            return {"sl": 1.0, "tp": 1.0, "trail": 1.0}

        factors = self.config.scenario_factors.get(scenario_type)
        if factors is None:
            return {"sl": 1.0, "tp": 1.0, "trail": 1.0}

        return {
            "sl": factors.get("sl", 1.0),
            "tp": factors.get("tp", 1.0),
            "trail": factors.get("trail", 1.0),
        }
