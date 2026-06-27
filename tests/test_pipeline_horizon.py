"""Tests for pipeline.py horizon integration."""
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from app.playbook.db import PlaybookDB
from app.playbook.pipeline import _check_playbook_exit_rules, _instruction_to_db_dict


class TestPipelineHorizon:
    """Pipeline horizon 路由测试."""

    def test_instruction_to_db_dict_includes_horizon(self):
        """_instruction_to_db_dict 应包含 horizon 字段."""
        inst = MagicMock()
        inst.symbol = "US.TSLA"
        inst.direction = "BUY"
        inst.entry_price = 100.0
        inst.stop_loss = 98.0
        inst.take_profit = 105.0
        inst.position_size = 10
        inst.risk_amount = 820.0
        inst.risk_reward = 2.5
        inst.confidence = 70
        inst.conditions = ["volume_expansion"]
        inst.thesis = "test"
        inst.sector = "Tech"
        inst.risks = []
        inst.data_quality = {"technical": "ok"}
        inst.entry_price_source = "test"
        inst.atr_source = "FIXED_2%"
        inst.decision_id = "abc123"
        inst.horizon = "long"
        inst.horizon_confidence = 0.8
        inst.horizon_reasoning = {"source": "fallback_rules"}

        regime_ctx = MagicMock()
        regime_ctx.regime.value = "RISK_ON"
        regime_ctx.vix = 18.0
        regime_ctx.spy_ma20 = 100.0
        regime_ctx.spy_ma50 = 99.0

        d = _instruction_to_db_dict(inst, regime_ctx, "2026-06-26")
        assert d["horizon"] == "long"
        assert d["horizon_confidence"] == 0.8
        assert d["horizon_reasoning"] == {"source": "fallback_rules"}

    def test_check_playbook_exit_rules_short_horizon_pre_close(self):
        """short horizon 盈利仓仍应 EOD 强平."""
        meta = {"horizon": "short"}
        exit_reason, exit_price, _, _ = _check_playbook_exit_rules(
            direction="LONG",
            entry_price=100.0,
            current_price=102.0,
            initial_sl=98.0,
            peak_price=102.0,
            meta=meta,
            held_minutes=300,
            minutes_to_close=5,
            minutes_from_open=390,
            entry_time_str="2026-06-26T10:00:00",
        )
        assert exit_reason == "PRE_CLOSE_EXIT"

    def test_check_playbook_exit_rules_long_horizon_skips_pre_close(self):
        """long horizon 盈利仓应跳过 EOD 强平."""
        meta = {"horizon": "long"}
        exit_reason, exit_price, _, _ = _check_playbook_exit_rules(
            direction="LONG",
            entry_price=100.0,
            current_price=102.0,
            initial_sl=98.0,
            peak_price=102.0,
            meta=meta,
            held_minutes=300,
            minutes_to_close=5,
            minutes_from_open=390,
            entry_time_str="2026-06-26T10:00:00",
        )
        assert exit_reason == ""