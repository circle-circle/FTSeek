# -*- coding: utf-8 -*-
"""Grace 期硬止损 reason 契约测试 (2026-08-16, DEB-fullchain P0-4).

契约: legacy 回退路径 `_check_via_legacy` 在 grace 期间必须放行
atr_stop_engine 实际发出的所有硬止损 reason。

背景: atr_stop_engine.py L403 实际发出 reason="HARD_STOP", 但旧白名单
{ATR_STOP, SL_HIT} 不含 HARD_STOP (且 SL_HIT 全仓无生产者) → grace 期
(min_hold 45min) 引擎硬止损被静默压制, 与 registry 路径
(exit_rule_registry.py hard_stop_reasons 含 HARD_STOP) 行为分裂。

修复: 白名单补 HARD_STOP。本测试锁定契约: 引擎硬止损 reason 全集必须
在 grace 白名单内, 防回归。
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import pytest
from unittest.mock import patch

from app.risk.position_exit_monitor import ExitMonitor
from app.risk.exit_rules.exit_rule_registry import ExitRuleContext


class FakePosition:
    """最小 Position 桩"""
    def __init__(self, symbol="US.TEST", direction="LONG", entry_price=100.0):
        self.symbol = symbol
        self.direction = direction
        self.avg_entry_price = entry_price
        self.size = 10.0
        self.take_profit_levels = []
        self.trail_state = {}
        self.metadata = {}
        self.current_sl = 0.0
        self.initial_sl = 0.0
        self.signal_id = "sig_test"
        self.strategy_id = "test"
        self.trace_id = "trace_test"
        self.owner_strategy = ""


def _run_legacy_with_stop_reason(monitor, stop_reason, in_grace=True):
    """让 _check_trailing_stop 返回指定 reason, 其他规则全静默, 走 legacy."""
    pos = FakePosition()
    stop_result = {"reason": stop_reason, "reason_cn": "契约测试",
                   "stop_price": 98.0, "priority": 1}
    with patch.object(monitor, "_check_scale_out", return_value=None), \
         patch.object(monitor, "_check_trailing_stop", return_value=stop_result), \
         patch.object(monitor, "_check_time_exit", return_value=None), \
         patch.object(monitor, "_check_pre_close_exit", return_value=None), \
         patch("app.risk.exit_rules.pre_close.check_max_hold_t5", return_value=None):
        return monitor._check_via_legacy(
            position=pos,
            current_price=97.0,
            entry_price=100.0,
            effective_atr=1.0,
            profit_atr_signed=-3.0,
            trail_state={},
            now_ts=time.time(),
            direction="LONG",
            pnl_pct=-0.03,
            held_minutes=5.0,      # < min_hold → 若不在 grace 白名单也压制
            policy_guardian=None,
            earnings_guard=None,
            _in_grace=in_grace,
        )


class TestGraceHardStopContract:
    """P0-4 契约: 引擎硬止损 reason 在 grace 期必须放行."""

    @pytest.fixture
    def monitor(self):
        return ExitMonitor()

    def test_hard_stop_passes_in_grace(self, monitor):
        """核心契约: atr_stop_engine 实际发出的 HARD_STOP 在 grace 期放行 (修复点)."""
        result = _run_legacy_with_stop_reason(monitor, "HARD_STOP", in_grace=True)
        assert result is not None, "HARD_STOP 在 grace 期被压制 — P0-4 回归!"
        assert result.get("reason") == "HARD_STOP"

    def test_atr_stop_passes_in_grace(self, monitor):
        """回归保护: ATR_STOP (旧白名单成员) 仍放行."""
        result = _run_legacy_with_stop_reason(monitor, "ATR_STOP", in_grace=True)
        assert result is not None and result.get("reason") == "ATR_STOP"

    def test_chandelier_suppressed_in_grace(self, monitor):
        """回归保护: 非硬止损 CHANDELIER 在 grace 期仍被压制 (不误伤)."""
        result = _run_legacy_with_stop_reason(monitor, "CHANDELIER", in_grace=True)
        assert result is None, "CHANDELIER 非硬止损不应在 grace 期放行"

    def test_breakeven_suppressed_in_grace(self, monitor):
        """回归保护: 非硬止损 BREAKEVEN 在 grace 期仍被压制."""
        result = _run_legacy_with_stop_reason(monitor, "BREAKEVEN", in_grace=True)
        assert result is None

    def test_legacy_hard_reasons_cover_registry_hard_reasons(self, monitor):
        """结构契约: legacy grace 白名单 ⊇ registry hard_stop_reasons 中的
        trailing_stop 类引擎 reason (HARD_STOP/ATR_STOP/SL_HIT),
        防止两条路径行为再次分裂。"""
        import inspect
        src = inspect.getsource(type(monitor)._check_via_legacy)
        registry_hard = ExitRuleContext().hard_stop_reasons
        # 引擎 (atr_stop_engine) 可产出的 trailing_stop reason 全集
        engine_reasons = {"HARD_STOP", "ATR_STOP", "SL_HIT",
                          "SCALE_OUT_FLOOR", "CHANDELIER", "BREAKEVEN"}
        # 硬止损子集 (registry 认定为硬的 ∩ 引擎可产出的)
        hard_subset = registry_hard & engine_reasons
        for reason in hard_subset:
            assert reason in src, (
                f"legacy grace 白名单缺 {reason} — registry 视为硬止损但 "
                f"legacy 回退路径会压制 (行为分裂)")
