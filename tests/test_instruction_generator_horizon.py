"""Tests for compute_instruction_values horizon behavior."""
from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from app.playbook.instruction_generator import compute_instruction_values


@dataclass
class MockTechnical:
    price: float = 100.0
    bid: float = 99.5
    ask: float = 100.5
    atr: float = 2.0
    quality: str = "ok"


@dataclass
class MockPlaybookData:
    technical = None

    def __post_init__(self):
        self.technical = MockTechnical()


class TestComputeInstructionValuesHorizon:
    """compute_instruction_values 按 horizon 取参数的测试."""

    def test_short_horizon_rr_ratio_1_0(self):
        data = MockPlaybookData()
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
        assert abs(tp_distance / sl_distance - 1.0) < 0.01
        assert values["horizon"] == "short"

    def test_long_horizon_rr_ratio_2_5(self):
        data = MockPlaybookData()
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
        assert abs(tp_distance / sl_distance - 2.5) < 0.01
        assert values["horizon"] == "long"
