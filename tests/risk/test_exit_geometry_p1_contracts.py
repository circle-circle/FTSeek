# -*- coding: utf-8 -*-
"""退出几何三联契约测试 (2026-08-16, DEB-fullchain P1-5/P1-10/P1-6).

P1-5: SL 契约抬线后必须重判 triggered (原用旧线判定, 抬线后不重判 → 永不退出)。
P1-10: regime 乘数仅在 policy 显式配置 trailing_stop.regime_multipliers 时生效;
       未配置不再用硬编码缺省 {normal:2.5,...} 压过 guardian 波动桶/SSOT。
P1-6: 报价异常 fallback 用最后有效报价 (prev_scan_price), 不再恒用开仓价。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import pytest
from unittest.mock import patch, MagicMock

from app.risk.exit_rules import trailing_stop as ts


class FakeGuardian:
    """guardian 桩: get_atr_multiplier 返回波动桶值 (low=1.5)."""
    def __init__(self, mult=1.5):
        self._mult = mult

    def get_atr_multiplier(self, symbol, direction, profit_atr=0, atr_pct=0):
        return self._mult


class FakePosition:
    def __init__(self, symbol="US.TEST", entry=100.0, initial_sl=0.0):
        self.symbol = symbol
        self.size = 10
        self.initial_sl = initial_sl
        self.market_regime = None
        self.trail_state = {}


def _call(monitor, pos, price, entry, atr, profit_atr, trail_state, direction, policy, initial_sl=0.0):
    pos.initial_sl = initial_sl
    return ts.check_trailing_stop(
        monitor, pos, price, entry, atr, profit_atr, trail_state,
        1000000.0, direction, policy,
    )


class TestP15SlFloorRetrigger:
    """P1-5 契约: SL 契约抬线后重判 triggered."""

    def test_long_breaches_floor_retriggers(self):
        """LONG: 价格跌破 initial_sl 地板但未越过更宽的旧止损线 → 必须触发退出."""
        pos = FakePosition()
        guard = FakeGuardian(mult=1.5)
        policy = MagicMock(policy={})
        # 低波动: entry=100, atr=0.7 → initial_sl = 100-1.5*0.7=98.95 (地板)
        # 引擎旧线更宽 (如 97.5), 价格 98.5: 跌破地板但未破旧线 → 旧实现不触发
        trail = {}
        with patch.object(ts, "compute_stop_lines") as mock_csl:
            r = MagicMock()
            r.final_stop = 97.5      # 引擎算出的宽线
            r.triggered = False      # 价格 98.5 未破 97.5
            r.atr_mult = 2.5; r.trail_mult = 2.0
            r.profit_atr_signed = -2.0; r.cost_buffer_price = 0.0
            mock_csl.return_value = r
            result = _call(None, pos, price=98.5, entry=100.0, atr=0.7,
                           profit_atr=-2.0, trail_state=trail, direction="LONG",
                           policy=guard, initial_sl=98.95)
        # 抬线到 98.95 后, 98.5<=98.95 → 必须触发
        assert result is not None, "跌破 initial_sl 地板必须触发退出 (P1-5)"
        assert result["reason"] == "ATR_STOP"

    def test_short_breaches_floor_retriggers(self):
        """SHORT 方向对称: 价格涨破 initial_sl 地板 → 必须触发."""
        pos = FakePosition()
        guard = FakeGuardian(mult=1.5)
        with patch.object(ts, "compute_stop_lines") as mock_csl:
            r = MagicMock()
            r.final_stop = 103.0     # 引擎宽线 (SHORT 止损在上方)
            r.triggered = False
            r.atr_mult = 2.5; r.trail_mult = 2.0
            r.profit_atr_signed = -2.0; r.cost_buffer_price = 0.0
            mock_csl.return_value = r
            result = _call(None, pos, price=101.5, entry=100.0, atr=0.7,
                           profit_atr=-2.0, trail_state={}, direction="SHORT",
                           policy=guard, initial_sl=101.05)
        assert result is not None, "SHORT 涨破地板必须触发 (P1-5)"

    def test_no_floor_no_retrigger(self):
        """无 initial_sl (floor=0) → 不重判, 维持引擎判定 (不误伤)."""
        pos = FakePosition()
        guard = FakeGuardian(mult=1.5)
        with patch.object(ts, "compute_stop_lines") as mock_csl:
            r = MagicMock()
            r.final_stop = 97.5
            r.triggered = False
            mock_csl.return_value = r
            result = _call(None, pos, price=98.5, entry=100.0, atr=0.7,
                           profit_atr=-2.0, trail_state={}, direction="LONG",
                           policy=guard, initial_sl=0)
        assert result is None


class TestP110RegimeMultOnlyWhenConfigured:
    """P1-10 契约: regime 乘数不再用硬编码缺省覆盖 guardian/SSOT."""

    def _run(self, policy_dict, guardian_mult=1.5):
        """返回引擎收到的 atr_mult (低波动 long, profit_atr=1.2 走 guardian 路径)."""
        pos = FakePosition()
        guard = FakeGuardian(mult=guardian_mult)
        guard.policy = policy_dict
        captured = {}
        def fake_csl(**kw):
            captured.update(kw)
            r = MagicMock()
            r.final_stop = 50.0; r.triggered = False
            return r
        with patch.object(ts, "compute_stop_lines", side_effect=fake_csl):
            _call(None, pos, price=101.2, entry=100.0, atr=1.0,
                  profit_atr=1.2, trail_state={}, direction="LONG",
                  policy=guard, initial_sl=0)
        return captured.get("atr_mult")

    def test_no_config_keeps_design_mult(self):
        """exit_policy 无 trailing_stop 键 → atr_mult = max(guardian, SSOT initial) = 2.0,
        不被硬编码缺省抬到 2.5 (设计值恢复: SSOT 地板是有意设计"追踪不紧于初始止损")."""
        policy_dict = {}  # 无 trailing_stop
        m = self._run(policy_dict, guardian_mult=1.5)
        assert m == pytest.approx(2.0), \
            f"未配置 regime_multipliers 时 atr_mult 应为 SSOT 地板 2.0 (得 {m}, P1-10 回归!)"

    def test_high_guardian_mult_not_lowered(self):
        """guardian 高桶 (3.0) 不被压低 (max 语义保留)."""
        policy_dict = {}
        m = self._run(policy_dict, guardian_mult=3.0)
        assert m == pytest.approx(3.0)

    def test_explicit_config_still_applies(self):
        """显式配置 regime_multipliers → 仍生效 (保留功能)."""
        policy_dict = {"trailing_stop": {"regime_multipliers": {"normal": 2.5}}}
        m = self._run(policy_dict, guardian_mult=1.5)
        assert m == pytest.approx(2.5), "显式配置应仍叠加 regime 乘数"


class TestP16PrevScanPriceUpdated:
    """P1-6 契约: _update_trail_state 写入 prev_scan_price."""

    def test_prev_scan_price_written(self):
        from app.risk.position_exit_monitor import ExitMonitor
        mon = ExitMonitor()
        pos = FakePosition()
        pos.trail_state = {"highest_price": 100.0, "lowest_price": 100.0}
        trail = dict(pos.trail_state)
        mon._update_trail_state(pos, current_price=103.5, trail_state=trail,
                                direction="LONG", entry_price=100.0, atr=0.0,
                                policy_guardian=None)
        assert pos.trail_state.get("prev_scan_price") == 103.5, \
            "prev_scan_price 必须随每轮扫描更新 (P1-6 根因)"

    def test_prev_scan_price_updated_even_no_trigger(self):
        """未触发退出也要写 prev (docstring 语义恢复)."""
        from app.risk.position_exit_monitor import ExitMonitor
        mon = ExitMonitor()
        pos = FakePosition()
        trail = {"highest_price": 100.0, "lowest_price": 100.0}
        mon._update_trail_state(pos, current_price=99.0, trail_state=trail,
                                direction="SHORT", entry_price=100.0, atr=0.0,
                                policy_guardian=None)
        assert pos.trail_state.get("prev_scan_price") == 99.0
