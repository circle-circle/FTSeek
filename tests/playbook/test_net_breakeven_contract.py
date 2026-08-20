# -*- coding: utf-8 -*-
"""净保本契约测试 (2026-08-17, 用户裁"保本单和豁免条件真正净保本, 包含费用, 避免白跑").

三处白跑源头 + 限价路径:
  ① SL_BREAKEVEN 市价平仓 fill 漂移 → 改挂限价单@净保本线 (meta._be_net_line_price)
  ② 豁免毛口径误判 (NVDA 8/17: 毛+0.013% 失去豁免 → DEAD_SIGNAL 出场 净-$3.51)
     → 5处豁免调用点切净口径 (_profit_net_pct)
  ③ PATIENT_EXIT_FILL 毛口径 → fill 腿切净口径

契约保证:
  A. 净保本线价格计算正确 (LONG/SHORT 方向对称)
  B. 净口径豁免: 微盈仓 (0 < 毛 < 费用) 被识别为"净水下" → 豁免
  C. 毛口径后向兼容: meta 无 _qty → 不启用净口径 → 行为不变
  D. 真正盈利仓 (毛 > 费用) 不豁免 → 正常退出
"""
import os
import sys
from datetime import timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from app.playbook.pipeline import _check_playbook_exit_rules  # noqa: E402
from app.utils.timezone import now_et  # noqa: E402

# fee_model 确定性锚: 10股@100 往返费 = $4.0706 → cushion = 0.40706%
# (与 app/execution/fee_model.py estimate_round_trip 一致, 测试前已验证)
_FEE_CUSHION_PCT = 0.40706  # 百分比


def _meta(source_type="PLAYBOOK_SIM", skip_allowed=False, qty=0):
    m = {
        "_log_tag": "test_net_be", "_symbol": "TEST", "cohort": "A",
        "horizon": "short", "_source_type": source_type,
        "_skip_allowed": skip_allowed,
    }
    if qty > 0:
        m["_qty"] = qty  # 注入股数 → 激活 D4 fee-aware cushion → 净口径
    return m


def _run(current_price, peak_price, held_minutes, entry_time_str,
         source_type="PLAYBOOK_SIM", direction="LONG", entry_price=100.0,
         initial_sl=95.0, minutes_to_close=30, minutes_from_open=20,
         skip_allowed=False, qty=0):
    meta = _meta(source_type, skip_allowed=skip_allowed, qty=qty)
    reason, exit_px, peak_out, be_trig = _check_playbook_exit_rules(
        direction=direction, entry_price=entry_price, current_price=current_price,
        initial_sl=initial_sl, peak_price=peak_price,
        meta=meta,
        held_minutes=held_minutes, minutes_to_close=minutes_to_close,
        minutes_from_open=minutes_from_open, entry_time_str=entry_time_str,
    )
    return reason, exit_px, peak_out, be_trig, meta


# ─────────────────────────────────────────────────────────────────────────────
# A. 净保本线价格计算
# ─────────────────────────────────────────────────────────────────────────────

class TestNetBreakevenLinePrice:
    """meta._be_net_line_price 计算契约."""

    def test_long_net_line_price(self):
        """LONG: 净保本线 = entry × (1 + 费率/100)."""
        entry_time = now_et().isoformat()
        # 触发 breakeven: peak > breakeven_at_pct (默认0.5%), 然后 current 回落到保本线
        # peak_price = 101.0 → peak_profit_pct = 1.0% > 0.5% → breakeven_triggered
        # current_price = 100.41 → 刚好在净保本线附近 (不触发SL, 只检查meta写入)
        _, _, _, _, meta = _run(
            current_price=100.41, peak_price=101.0,
            held_minutes=30, entry_time_str=entry_time,
            direction="LONG", entry_price=100.0, qty=10,
        )
        net_line = meta.get("_be_net_line_price")
        assert net_line is not None, "有_qty时应写入_be_net_line_price"
        expected = round(100.0 * (1 + _FEE_CUSHION_PCT / 100.0), 4)
        assert abs(net_line - expected) < 0.0001, \
            f"LONG净保本线应为{expected}, got {net_line}"

    def test_short_net_line_price(self):
        """SHORT: 净保本线 = entry × (1 − 费率/100)."""
        entry_time = now_et().isoformat()
        _, _, _, _, meta = _run(
            current_price=99.59, peak_price=99.0,  # SHORT: peak低于entry
            held_minutes=30, entry_time_str=entry_time,
            direction="SHORT", entry_price=100.0, qty=10,
        )
        net_line = meta.get("_be_net_line_price")
        assert net_line is not None, "有_qty时应写入_be_net_line_price"
        expected = round(100.0 * (1 - _FEE_CUSHION_PCT / 100.0), 4)
        assert abs(net_line - expected) < 0.0001, \
            f"SHORT净保本线应为{expected}, got {net_line}"

    def test_no_qty_no_net_line(self):
        """无_qty → 不启用D4 → 无_be_net_line_price (毛口径后向兼容)."""
        entry_time = now_et().isoformat()
        _, _, _, _, meta = _run(
            current_price=100.41, peak_price=101.0,
            held_minutes=30, entry_time_str=entry_time,
            direction="LONG", entry_price=100.0, qty=0,
        )
        assert "_be_net_line_price" not in meta, \
            "无_qty时不应写入_be_net_line_price"
        assert "_be_cushion_fee_aware" not in meta, \
            "无_qty时不应写入_be_cushion_fee_aware"


# ─────────────────────────────────────────────────────────────────────────────
# B. 净口径豁免 — DEAD_SIGNAL (NVDA 8/17 实弹复现)
# ─────────────────────────────────────────────────────────────────────────────

class TestNetCaliberDeadSignalExempt:
    """DEAD_SIGNAL 豁免: 微盈仓 (0<毛<费用) 净口径下=净水下 → 豁免."""

    def _nvda_scenario(self, qty):
        """NVDA 8/17 场景: SHORT, 毛+0.013%, held=800min, peak<0.2%.
        SHORT 的 initial_sl 必须在入场价上方 (105), 否则 SL_INITIAL 先触发."""
        entry = (now_et() - timedelta(days=1)).isoformat()
        # SHORT: profit_pct = (entry - current) / entry * 100
        # 毛+0.013% → current = entry × (1 - 0.013/100) = 99.987
        return _run(
            current_price=99.987, peak_price=99.9,  # peak_profit_pct = +0.1% < 0.2%
            held_minutes=800, entry_time_str=entry,
            direction="SHORT", entry_price=100.0, initial_sl=105.0, qty=qty,
        )

    def test_micro_profit_exempt_net_caliber(self):
        """净口径: 毛+0.013% < 费用0.407% → 净-0.394% < 0 → 豁免, 不退出."""
        reason, *_ = self._nvda_scenario(qty=10)
        assert reason == "", \
            f"净口径下微盈仓应豁免DEAD_SIGNAL, got {reason}"

    def test_micro_profit_fires_brute_caliber(self):
        """毛口径后向兼容: 无_qty → 毛+0.013% > 0 → 不豁免 → DEAD_SIGNAL_EXIT."""
        reason, *_ = self._nvda_scenario(qty=0)
        assert reason == "DEAD_SIGNAL_EXIT", \
            f"毛口径下微盈仓不豁免, got {reason}"

    def test_underwater_exempt_both_calibers(self):
        """水下仓 (毛<0): 净口径下仍豁免 (净更负)."""
        entry = (now_et() - timedelta(days=1)).isoformat()
        # SHORT: 毛-0.5% → current = 100.5; SL在入场价上方 (105) 不触发
        reason, *_ = _run(
            current_price=100.5, peak_price=99.9,
            held_minutes=800, entry_time_str=entry,
            direction="SHORT", entry_price=100.0, initial_sl=105.0, qty=10,
        )
        assert reason == "", f"水下仓应豁免, got {reason}"

    # 注: "真正盈利仓不豁免" 在 DEAD_SIGNAL 分支数学上不可构造 —
    # DEAD_SIGNAL 要求 peak_profit_pct<0.2%, 而往返费≈0.407% > 0.2%,
    # 死信号窗口内 净盈亏 = 毛−费用 恒 < 0 (微盈仓在净口径下全是净水下).
    # "真正盈利→不豁免" 的可测场景在 PRE_CLOSE (见 TestNetCaliberPreCloseExempt).


# ─────────────────────────────────────────────────────────────────────────────
# C. 净口径豁免 — PRE_CLOSE (EOD)
# ─────────────────────────────────────────────────────────────────────────────

class TestNetCaliberPreCloseExempt:
    """PRE_CLOSE 豁免: 微盈仓净口径下=净水下 → 豁免持夜."""

    def _eod_scenario(self, qty, direction="LONG"):
        entry = now_et().isoformat()  # 当日入场
        # LONG: 毛+0.013% → current = 100.013
        # SHORT: 毛+0.013% → current = 99.987 (SL须在入场价上方=105)
        current = 100.013 if direction == "LONG" else 99.987
        sl = 95.0 if direction == "LONG" else 105.0
        return _run(
            current_price=current, peak_price=100.1 if direction == "LONG" else 99.9,
            held_minutes=300, entry_time_str=entry,
            direction=direction, entry_price=100.0, initial_sl=sl, qty=qty,
            minutes_to_close=5,  # 收盘前5min → PRE_CLOSE 窗口
            minutes_from_open=385,  # 已过开盘
        )

    def test_micro_profit_exempt_eod_long(self):
        """LONG微盈仓: 净口径下豁免PRE_CLOSE."""
        reason, *_ = self._eod_scenario(qty=10, direction="LONG")
        assert reason == "", f"净口径下微盈仓应豁免PRE_CLOSE, got {reason}"

    def test_micro_profit_exempt_eod_short(self):
        """SHORT微盈仓: 净口径下豁免PRE_CLOSE."""
        reason, *_ = self._eod_scenario(qty=10, direction="SHORT")
        assert reason == "", f"净口径下微盈仓应豁免PRE_CLOSE, got {reason}"

    def test_micro_profit_fires_eod_brute(self):
        """毛口径: 无_qty → 不豁免 → PRE_CLOSE_EXIT."""
        reason, *_ = self._eod_scenario(qty=0, direction="LONG")
        assert reason == "PRE_CLOSE_EXIT", \
            f"毛口径下微盈仓不豁免, got {reason}"

    def test_truly_profitable_not_exempt_pre_close(self):
        """真正盈利 (毛+0.62% > 费用0.407%): 净+0.213% > 0 → 不豁免 → PRE_CLOSE_EXIT.
        (DEAD_SIGNAL 分支此场景数学不可构造, PRE_CLOSE 是"真盈利不豁免"的可测点.)
        2026-08-20 (方案A) 场景重锚: 原 current=100.5/peak=100.6 在 slip 垫线
        (fee 0.407 + slip 0.2 → 100.607) 下方, 会先触发 SL_BREAKEVEN (方案A 预期行为,
        见 TestSlipCushionContract) — 本测试目的不变 (真盈利仓不豁免 PRE_CLOSE),
        场景上移到垫线上方: current=100.62 > 100.607+1bp容差, SL 不触发."""
        entry_time = now_et().isoformat()
        reason, *_ = _run(
            current_price=100.62, peak_price=100.7,
            held_minutes=300, entry_time_str=entry_time,
            direction="LONG", entry_price=100.0, qty=10,
            minutes_to_close=5, minutes_from_open=385,
        )
        assert reason == "PRE_CLOSE_EXIT", \
            f"真正盈利仓不豁免, got {reason}"


# ─────────────────────────────────────────────────────────────────────────────
# D. 净口径豁免 — TIME_EXIT
# ─────────────────────────────────────────────────────────────────────────────

class _FakeCfgFlipTimeExitGate:
    """Fake config: 翻转 default 因子的 time_exit_only_if_loss=False.

    默认 only_if_loss=True 时毛盈仓根本不进 TIME_EXIT 判定门 (毛口径退出门,
    故意保留), 净口径豁免分支不可达 — 翻转后才能测到豁免判定本身.
    其余键透传真 config (breakeven_fee_aware/dead_signal/loser_overnight).
    """

    def __init__(self):
        from app.playbook.config import get_playbook_config as _real_get
        self._real = _real_get()

    def get(self, key, default=None):
        return self._real.get(key, default)

    def get_horizon_top(self, key, default=None):
        if key == "factor_exit_rules":
            rules = dict(self._real.get_horizon_top(key, {}) or {})
            rules["default"] = dict(rules.get("default", {}) or {})
            rules["default"]["time_exit_only_if_loss"] = False
            rules["default"]["time_exit_hours"] = 2.0  # 强制覆盖 (真实值6.0, held=130min达不到)
            return rules
        return self._real.get_horizon_top(key, default)


class TestNetCaliberTimeExitExempt:
    """TIME_EXIT 豁免: 微盈仓净口径下=净水下 → 豁免.

    需 monkeypatch 翻转 time_exit_only_if_loss (默认True时毛盈仓被退出门挡住,
    豁免分支不可达). 毛口径退出门保留是设计 (切净口径会把微盈仓市价卖=白跑).
    """

    @pytest.fixture()
    def flip_gate(self, monkeypatch):
        fake = _FakeCfgFlipTimeExitGate()
        monkeypatch.setattr("app.playbook.config.get_playbook_config", lambda: fake)
        return fake

    def test_micro_profit_exempt_time_exit(self, flip_gate):
        """TIME_EXIT: 毛+0.013%, held>2h → 净口径豁免.
        peak=0.3%≥0.2% 隔离 DEAD_SIGNAL 分支 (代码顺序 DEAD_SIGNAL 在 TIME_EXIT 前)."""
        entry = now_et().isoformat()
        # LONG: 毛+0.013% → current = 100.013; peak=100.3 → peak_pct=0.3% ≥ 0.2%
        reason, *_ = _run(
            current_price=100.013, peak_price=100.3,
            held_minutes=130, entry_time_str=entry,
            direction="LONG", entry_price=100.0, qty=10,
            minutes_to_close=30, minutes_from_open=130,
        )
        assert reason == "", f"净口径下微盈仓应豁免TIME_EXIT, got {reason}"

    def test_micro_profit_fires_time_exit_brute(self, flip_gate):
        """毛口径: 无_qty → 净口径不启用 → 毛+0.013%≥0 不豁免 → TIME_EXIT."""
        entry = now_et().isoformat()
        reason, *_ = _run(
            current_price=100.013, peak_price=100.3,
            held_minutes=130, entry_time_str=entry,
            direction="LONG", entry_price=100.0, qty=0,
            minutes_to_close=30, minutes_from_open=130,
        )
        assert reason == "TIME_EXIT", f"毛口径下微盈仓不豁免, got {reason}"


# ─────────────────────────────────────────────────────────────────────────────
# E. 净口径豁免 — MAX_HOLD
# ─────────────────────────────────────────────────────────────────────────────

class _FakeCfgMaxHold:
    """Fake config: 打开 overnight_hold_enabled (否则 pipeline L5839 强制
    _skip_allowed=False, MAX_HOLD 分支整段跳过, 豁免测试假绿).
    与 test_w_week_dead_signal_contract.py::_FakeCfg 同款模式."""

    def __init__(self):
        from app.playbook.config import get_playbook_config as _real_get
        self._real = _real_get()

    def get(self, key, default=None):
        if key == "overnight_hold_enabled":
            return True
        return self._real.get(key, default)

    def get_horizon_top(self, key, default=None):
        return self._real.get_horizon_top(key, default)


class TestNetCaliberMaxHoldExempt:
    """MAX_HOLD 豁免: 微盈仓净口径下=净水下 → 豁免."""

    @pytest.fixture()
    def overnight_cfg(self, monkeypatch):
        fake = _FakeCfgMaxHold()
        monkeypatch.setattr("app.playbook.config.get_playbook_config", lambda: fake)
        return fake

    def test_micro_profit_exempt_max_hold(self, overnight_cfg):
        """MAX_HOLD: 毛+0.013%, 超horizon max_hold(short=1日) → 净口径豁免.
        peak=0.3%≥0.2% 隔离 DEAD_SIGNAL; entry=3日前 (超short 1日cap, 但仍在
        loser_overnight 5日窗口内 — 12日前会同时超5日窗口, 豁免失效)."""
        entry = (now_et() - timedelta(days=3)).isoformat()
        reason, *_ = _run(
            current_price=100.013, peak_price=100.3,
            held_minutes=800, entry_time_str=entry,
            direction="LONG", entry_price=100.0, qty=10,
            skip_allowed=True,  # MAX_HOLD 需要 _skip_allowed=True
        )
        assert reason == "", f"净口径下微盈仓应豁免MAX_HOLD, got {reason}"

    def test_micro_profit_fires_max_hold_brute(self, overnight_cfg):
        """毛口径: 无_qty → 不豁免 → MAX_HOLD."""
        entry = (now_et() - timedelta(days=3)).isoformat()
        reason, *_ = _run(
            current_price=100.013, peak_price=100.3,
            held_minutes=800, entry_time_str=entry,
            direction="LONG", entry_price=100.0, qty=0,
            skip_allowed=True,
        )
        assert reason == "MAX_HOLD", f"毛口径下微盈仓不豁免, got {reason}"


# ─────────────────────────────────────────────────────────────────────────────
# F. SL_BREAKEVEN 限价路径契约 (meta._be_net_line_price 供 monitor 消费)
# ─────────────────────────────────────────────────────────────────────────────

class TestSLBreakevenLimitPriceContract:
    """SL_BREAKEVEN 触发时 meta._be_net_line_price 供 monitor 挂限价单."""

    def test_breakeven_triggered_sets_net_line(self):
        """peak > breakeven_at_pct → breakeven_triggered=True + _be_net_line_price 写入."""
        entry_time = now_et().isoformat()
        # peak_price=101.0 → peak_profit_pct=1.0% > 0.5% → breakeven_triggered
        reason, _, _, be_trig, meta = _run(
            current_price=100.5, peak_price=101.0,
            held_minutes=30, entry_time_str=entry_time,
            direction="LONG", entry_price=100.0, qty=10,
        )
        assert be_trig, "peak>breakeven_at_pct时应触发breakeven"
        net_line = meta.get("_be_net_line_price")
        assert net_line is not None, "breakeven触发时应写入净保本线"
        # 净保本线应高于入场价 (LONG)
        assert net_line > 100.0, f"LONG净保本线应>entry, got {net_line}"

    def test_breakeven_not_triggered_no_net_line(self):
        """peak < breakeven_at_pct → breakeven_triggered=False (但net_line仍写入, 供后续用)."""
        entry_time = now_et().isoformat()
        # peak_price=100.3 → peak_profit_pct=0.3% < 0.5% → 不触发
        _, _, _, be_trig, meta = _run(
            current_price=100.2, peak_price=100.3,
            held_minutes=30, entry_time_str=entry_time,
            direction="LONG", entry_price=100.0, qty=10,
        )
        assert not be_trig, "peak<breakeven_at_pct时不应触发breakeven"
        # net_line 仍写入 (D4 cushion 算出即写, 不依赖 breakeven_triggered)
        assert meta.get("_be_net_line_price") is not None, \
            "D4 cushion算出时应写入净保本线"


# ─────────────────────────────────────────────────────────────────────────────
# G. 方案A 滑点垫契约 (2026-08-20, 用户裁"同意先方案A")
# ─────────────────────────────────────────────────────────────────────────────

class _FakeCfgSlip:
    """Fake config: 注入 breakeven_slip_cushion_pct (其余键透传真 config)."""

    def __init__(self, slip=0.2):
        from app.playbook.config import get_playbook_config as _real_get
        self._real = _real_get()
        self._slip = slip

    def get(self, key, default=None):
        if key == "breakeven_slip_cushion_pct":
            return self._slip
        return self._real.get(key, default)

    def get_horizon_top(self, key, default=None):
        return self._real.get_horizon_top(key, default)


class TestSlipCushionContract:
    """方案A: 触发线=费用线+滑点垫; 限价地板仍=纯费用线 (翻主动侧几何).

    实弹依据 8/20: 限价成交3笔全净正 / 8s超时降级市价3笔全净负(-40.36).
    """

    @pytest.fixture()
    def slip_cfg(self, monkeypatch):
        fake = _FakeCfgSlip(slip=0.2)
        monkeypatch.setattr("app.playbook.config.get_playbook_config", lambda: fake)
        return fake

    def test_trigger_line_raises_by_slip_long(self, slip_cfg):
        """LONG: 回踩到 (fee+slip) 垫线 → SL_BREAKEVEN. 无垫时该价位不触发."""
        entry_time = now_et().isoformat()
        # entry=100, fee≈0.40706%, slip=0.2% → 触发线≈100.607; current=100.60 < 线 → 触发
        reason, _, _, be_trig, _ = _run(
            current_price=100.60, peak_price=101.0,
            held_minutes=30, entry_time_str=entry_time,
            direction="LONG", entry_price=100.0, qty=10,
        )
        assert be_trig, "peak=1.0% 应已武装"
        assert reason == "SL_BREAKEVEN", f"回踩垫线应触发, got {reason}"

    def test_trigger_line_raises_by_slip_short(self, slip_cfg):
        """SHORT 方向对称: 回踩到 (fee+slip) 垫线 → SL_BREAKEVEN."""
        entry_time = now_et().isoformat()
        # SHORT entry=100, peak=99 (盈1%); 垫线=100×(1−0.607%)≈99.393
        # current=99.40 ≥ 99.393 → 触发. initial_sl=105 (SHORT 止损在上方)
        reason, _, _, be_trig, _ = _run(
            current_price=99.40, peak_price=99.0,
            held_minutes=30, entry_time_str=entry_time,
            direction="SHORT", entry_price=100.0, initial_sl=105.0, qty=10,
        )
        assert be_trig
        assert reason == "SL_BREAKEVEN", f"SHORT 回踩垫线应触发, got {reason}"

    def test_limit_floor_stays_fee_only(self, slip_cfg):
        """限价地板 _be_net_line_price 只含 fee 不含 slip — 翻主动侧几何的前提."""
        entry_time = now_et().isoformat()
        _, _, _, _, meta = _run(
            current_price=100.60, peak_price=101.0,
            held_minutes=30, entry_time_str=entry_time,
            direction="LONG", entry_price=100.0, qty=10,
        )
        net_line = meta.get("_be_net_line_price")
        expected_fee_line = round(100.0 * (1 + _FEE_CUSHION_PCT / 100.0), 4)
        assert net_line is not None
        assert abs(net_line - expected_fee_line) < 0.0001, \
            f"限价地板应为纯费用线 {expected_fee_line}, got {net_line} (slip 不得混入)"

    def test_no_slip_default_unchanged(self, monkeypatch):
        """slip=0 (回滚态) → 行为与改动前一致: 100.60 高于费用线 → 不出场.
        (显式注入 0: yaml 生产值已是 0.2, 直读真配置会测到垫线触发.
        fake 必须在 monkeypatch 之前构造 — 否则 __init__ 回调已 patch 的函数, 无限递归.)"""
        fake = _FakeCfgSlip(slip=0.0)
        monkeypatch.setattr("app.playbook.config.get_playbook_config", lambda: fake)
        entry_time = now_et().isoformat()
        reason, *_ = _run(
            current_price=100.60, peak_price=101.0,
            held_minutes=30, entry_time_str=entry_time,
            direction="LONG", entry_price=100.0, qty=10,
        )
        assert reason == "", f"slip=0 时 100.60 不应触发, got {reason}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
