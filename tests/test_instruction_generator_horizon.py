"""Tests for compute_instruction_values horizon behavior."""
from dataclasses import dataclass

import pytest

from app.playbook.data_collector import PlaybookData, TechnicalData, SentimentData
from app.playbook.instruction_generator import compute_instruction_values


def _make_data(atr_14: float = 2.0, vix: float = 15.0) -> PlaybookData:
    return PlaybookData(
        symbol="US.TSLA",
        technical=TechnicalData(price=100.0, bid=99.5, ask=100.5, atr_14=atr_14, quality="ok"),
        sentiment=SentimentData(vix=vix, quality="ok"),
    )


class TestComputeInstructionValuesHorizon:
    """compute_instruction_values 按 horizon 取参数的测试."""

    def test_short_horizon_uses_vol_bucket(self):
        # atr_pct = 2 / 100.5 ≈ 1.99% → medium bucket → tp_multiplier=1.8, sl≈1.8%
        data = _make_data(atr_14=2.0)
        decision = {
            "symbol": "US.TSLA",
            "direction": "BUY",
            "selected_conditions": ["volume_expansion"],
            "thesis_template": "breakout",
        }
        values = compute_instruction_values(decision, data, total_equity=82000.0, horizon="short")

        entry = values["entry_price"]
        stop = values["stop_loss"]
        take = values["take_profit"]

        sl_distance = entry - stop
        tp_distance = take - entry
        # medium bucket tp_multiplier is 1.8
        assert abs(tp_distance / sl_distance - 1.8) < 0.05
        assert values["horizon"] == "short"

    def test_long_horizon_uses_vol_bucket(self):
        # medium bucket for long horizon → tp_multiplier=2.0
        data = _make_data(atr_14=2.0)
        decision = {
            "symbol": "US.TSLA",
            "direction": "BUY",
            "selected_conditions": ["volume_expansion"],
            "thesis_template": "breakout",
        }
        values = compute_instruction_values(decision, data, total_equity=82000.0, horizon="long")

        entry = values["entry_price"]
        stop = values["stop_loss"]
        take = values["take_profit"]

        sl_distance = entry - stop
        tp_distance = take - entry
        assert abs(tp_distance / sl_distance - 2.0) < 0.05
        assert values["horizon"] == "long"
