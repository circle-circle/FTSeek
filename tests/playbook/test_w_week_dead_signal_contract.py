# -*- coding: utf-8 -*-
"""W_WEEK 方案B 契约测试 (2026-08-16, DEB-20260815 §39).

用户裁"方案B": SIM 水下仓豁免 DEAD_SIGNAL_EXIT (含 day-1 盘中) + MAX_HOLD 排雷。
根因: W_WEEK 水下隔夜仓的 MFE 是生命周期累计 — 入场日 MFE<0.2% 的仓,
次日开盘 held(墙钟)≫60min 且 MFE 恒<0.2% → DEAD_SIGNAL 必触发 →
W_WEEK 的 5 日修复空间被截断成 2 天 (回放 n=6 子集 +$698 全灭)。

豁免范围 = _loser_overnight_exempt (仅 PLAYBOOK_SIM + 水下 + ≤5交易日)。
LIVE 零影响; SL 仍最高优先级兜底。
"""
import os
import sys
from datetime import timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from app.playbook.pipeline import _check_playbook_exit_rules  # noqa: E402
from app.utils.timezone import now_et  # noqa: E402


def _meta(source_type="PLAYBOOK_SIM", skip_allowed=False):
    return {
        "_log_tag": "test", "_symbol": "TEST", "cohort": "A",
        "horizon": "short", "_source_type": source_type,
        # MAX_HOLD 分支需要 _skip_allowed=True (生产由 monitor 按 cohort B 注入);
        # 不注入则规则5整段跳过, 豁免/触发测试都会假绿.
        "_skip_allowed": skip_allowed,
    }


def _run(current_price, peak_price, held_minutes, entry_time_str,
         source_type="PLAYBOOK_SIM", direction="LONG", entry_price=100.0,
         initial_sl=95.0, minutes_to_close=30, minutes_from_open=20,
         skip_allowed=False):
    return _check_playbook_exit_rules(
        direction=direction, entry_price=entry_price, current_price=current_price,
        initial_sl=initial_sl, peak_price=peak_price,
        meta=_meta(source_type, skip_allowed=skip_allowed),
        held_minutes=held_minutes, minutes_to_close=minutes_to_close,
        minutes_from_open=minutes_from_open, entry_time_str=entry_time_str,
    )


class TestDeadSignalWWeekExempt:
    """规则3.3 DEAD_SIGNAL 的 W_WEEK 豁免边界.

    2026-09-02 钉子重锚 (RFC-073): 隔夜仓 DEAD_SIGNAL 生效口径改"今日开盘以来
    分钟数"后, 隔夜场景必须给 minutes_from_open≥dead_signal_exit_minutes (61)
    让时间门先过, 豁免边界才在被测位置 (否则 20<60 时间门挡住, 豁免测试假绿、
    触发测试假断). 时间口径本身由 test_rfc073_dead_signal_overnight.py 专钉.
    """

    def test_sim_underwater_overnight_exempt(self):
        """核心: SIM水下隔夜仓 (MFE=0.1%<0.2%, held=800min) → 豁免, 不退出."""
        entry = (now_et() - timedelta(days=1)).isoformat()
        reason, *_ = _run(current_price=99.0, peak_price=100.1,
                          held_minutes=800, entry_time_str=entry,
                          minutes_from_open=61)
        assert reason == "", f"W_WEEK水下仓应豁免DEAD_SIGNAL, got {reason}"

    def test_live_underwater_dead_signal_fires(self):
        """LIVE 零影响: 同场景 LIVE 仓仍被 DEAD_SIGNAL 杀掉."""
        entry = (now_et() - timedelta(days=1)).isoformat()
        reason, *_ = _run(current_price=99.0, peak_price=100.1,
                          held_minutes=800, entry_time_str=entry,
                          source_type="PLAYBOOK_LIVE", minutes_from_open=61)
        assert reason == "DEAD_SIGNAL_EXIT", f"LIVE不受豁免影响, got {reason}"

    def test_sim_nonunderwater_dead_signal_fires(self):
        """水下边界: profit>=0 (微盈) 不在豁免范围, DEAD_SIGNAL 仍触发."""
        entry = (now_et() - timedelta(days=1)).isoformat()
        reason, *_ = _run(current_price=100.05, peak_price=100.1,
                          held_minutes=800, entry_time_str=entry,
                          minutes_from_open=61)
        assert reason == "DEAD_SIGNAL_EXIT", f"非水下仓不豁免, got {reason}"

    def test_sim_underwater_day1_also_exempt_planB(self):
        """方案B语义锁定: day-1 盘中 SIM水下仓 (held=90min) 也豁免."""
        entry = now_et().isoformat()
        reason, *_ = _run(current_price=99.5, peak_price=100.1,
                          held_minutes=90, entry_time_str=entry)
        assert reason == "", f"方案B: day-1水下仓也豁免, got {reason}"

    def test_day5_expiry_dead_signal_returns(self):
        """5日窗口边界: 超窗 (入场12历日前≥8交易日) → 豁免失效, DEAD_SIGNAL 恢复."""
        entry = (now_et() - timedelta(days=12)).isoformat()
        reason, *_ = _run(current_price=99.0, peak_price=100.1,
                          held_minutes=800, entry_time_str=entry,
                          minutes_from_open=61)
        assert reason == "DEAD_SIGNAL_EXIT", f"超5日窗口后应恢复DEAD_SIGNAL, got {reason}"

    def test_sl_still_dominates_exempt_position(self):
        """SL 兜底优先级不变: 豁免仓触及 initial_sl 仍被 SL_INITIAL 杀掉."""
        entry = (now_et() - timedelta(days=1)).isoformat()
        reason, *_ = _run(current_price=94.0, peak_price=100.1,
                          held_minutes=800, entry_time_str=entry, initial_sl=95.0)
        assert reason == "SL_INITIAL", f"SL兜底必须先于豁免, got {reason}"


class _FakeCfg:
    """最小 PlaybookConfig 替身 (monkeypatch 用)."""

    def __init__(self, d):
        self._d = d

    def get(self, key, default=None):
        return self._d.get(key, default)

    def get_horizon_top(self, key, default=None):
        return default


class TestMaxHoldWWeekExempt:
    """规则5 MAX_HOLD 的 W_WEEK 豁免 (排雷: 防 overnight_hold_enabled 打开后爆炸)."""

    @pytest.fixture()
    def overnight_cfg(self, monkeypatch):
        fake = _FakeCfg({
            "overnight_hold_enabled": True,   # 让 _skip_allowed 存活
            "loser_overnight_enabled": True,
            "loser_overnight_max_hold_days": 5,
        })
        monkeypatch.setattr("app.playbook.config.get_playbook_config", lambda: fake)
        return fake

    def test_max_hold_exempt_underwater(self, overnight_cfg):
        """隔夜开关打开后: 水下仓达 horizon max_hold(1日) 仍豁免 (W_WEEK 5日窗口)."""
        entry = (now_et() - timedelta(days=3)).isoformat()
        reason, *_ = _run(current_price=99.0, peak_price=100.1,
                          held_minutes=800, entry_time_str=entry, skip_allowed=True)
        assert reason == "", f"W_WEEK水下仓应豁免MAX_HOLD, got {reason}"

    def test_max_hold_fires_when_profit(self, overnight_cfg):
        """豁免边界: 盈利仓不豁免, MAX_HOLD 正常触发."""
        entry = (now_et() - timedelta(days=3)).isoformat()
        reason, *_ = _run(current_price=100.5, peak_price=100.6,
                          held_minutes=800, entry_time_str=entry, skip_allowed=True)
        assert reason == "MAX_HOLD", f"盈利仓不豁免MAX_HOLD, got {reason}"
