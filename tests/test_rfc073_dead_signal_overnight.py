"""RFC-073 测试钉: DEAD_SIGNAL 隔夜仓时间口径对齐 (2026-09-02).

病灶: 隔夜仓 held_minutes=入场以来墙钟 (数千分钟), 次日第一轮巡检恒≥60min,
      死信号规则退化为"开盘第一眼秒杀", 时间维度消失.
修复: 隔夜仓用"今日开盘以来分钟数" (同 RFC-064 TIME_EXIT 口径).

回放证据: 6笔隔夜死信号仓 1min bar 反事实 +$97 (悲观序);
          真死信号 (峰值从未≥0.2%) 照杀.
"""
from unittest.mock import patch

import pytest

import app.playbook.pipeline as pipeline_mod
from app.playbook.pipeline import _check_playbook_exit_rules

class _CfgStub(dict):
    """get_playbook_config 桩: dict.get + horizon 段空实现 (本测试不触 factor 规则)."""
    def get_horizon_top(self, key, default=None):
        return self.get(key, default if default is not None else {})




CFG_BASE = {
    "dead_signal_exit_enabled": True,
    "dead_signal_exit_minutes": 60,
    "dead_signal_exit_mfe_pct": 0.2,
    "dead_signal_reentry_extension_min": 0,
    "dead_signal_overnight_daily_reset_enabled": True,
    "loser_overnight_enabled": False,  # 关豁免, 隔离测时间口径
    "overnight_hold_enabled": True,
    "time_exit_overnight_daily_reset_enabled": True,
    "early_dead_signal_shadow_enabled": False,
    "patient_exit_enabled": False,
}
CFG_RESET_OFF = dict(CFG_BASE, dead_signal_overnight_daily_reset_enabled=False)


def _run(cfg, meta, held_minutes, minutes_from_open, peak_profit_pct=0.1,
         entry_time="2026-08-31T09:40:00-04:00", now_patched=True):
    """调用退出规则, patch 配置与"当前时间"(模拟次日开盘后)."""
    with patch("app.playbook.config.get_playbook_config", return_value=_CfgStub(cfg)), \
         patch("app.playbook.pipeline._is_overnight_position", return_value=True):
        return _check_playbook_exit_rules(
            direction="LONG", entry_price=100.0, current_price=100.1,
            initial_sl=95.0, peak_price=100.0 * (1 + peak_profit_pct / 100),
            meta=meta, held_minutes=held_minutes, minutes_to_close=300.0,
            minutes_from_open=minutes_from_open, entry_time_str=entry_time,
        )


class TestDeadSignalOvernightReset:
    """隔夜仓: 墙钟7183min + 开盘仅1分钟 → 不应触发 (获得60min观察窗)."""

    def test_overnight_open_first_minute_not_killed(self):
        meta = {"cohort": "A", "horizon": "short"}
        reason, *_ = _run(CFG_BASE, meta, held_minutes=7183.4, minutes_from_open=1.0)
        assert reason == ""  # 存活: 窗口未到

    def test_overnight_open_59min_not_killed(self):
        meta = {"cohort": "A", "horizon": "short"}
        reason, *_ = _run(CFG_BASE, meta, held_minutes=7240.0, minutes_from_open=59.0)
        assert reason == ""

    def test_overnight_open_60min_low_peak_killed(self):
        """60min窗走完 + 峰值从未达标 → 真死信号照杀 (修复不放过死单)."""
        meta = {"cohort": "A", "horizon": "short"}
        reason, *_ = _run(CFG_BASE, meta, held_minutes=7241.0, minutes_from_open=61.0,
                          peak_profit_pct=0.1)
        assert reason == "DEAD_SIGNAL_EXIT"

    def test_overnight_peak_reached_survives(self):
        """峰值≥0.2% (信号兑现过) → 条件不满足, 存活."""
        meta = {"cohort": "A", "horizon": "short"}
        reason, *_ = _run(CFG_BASE, meta, held_minutes=7241.0, minutes_from_open=61.0,
                          peak_profit_pct=0.25)
        assert reason != "DEAD_SIGNAL_EXIT"

    def test_check_minutes_records_effective_caliber(self):
        """遥测 check_minutes = 生效口径 (61), 非墙钟 (7241)."""
        meta = {"cohort": "A", "horizon": "short"}
        reason, *_ = _run(CFG_BASE, meta, held_minutes=7241.0, minutes_from_open=61.0,
                          peak_profit_pct=0.1)
        assert reason == "DEAD_SIGNAL_EXIT"
        assert meta["dead_signal_exit"]["check_minutes"] == 61.0
        assert meta["dead_signal_exit"]["wall_minutes"] == 7241.0


class TestDeadSignalOvernightResetOff:
    """开关关闭 → 回退旧口径 (墙钟), 隔夜仓开盘秒杀 (历史行为)."""

    def test_reset_off_overnight_killed_at_open(self):
        meta = {"cohort": "A", "horizon": "short"}
        reason, *_ = _run(CFG_RESET_OFF, meta, held_minutes=7183.4, minutes_from_open=1.0,
                          peak_profit_pct=0.1)
        assert reason == "DEAD_SIGNAL_EXIT"


class TestDeadSignalIntradayUnchanged:
    """日内仓: 口径不变 (墙钟=开盘分钟, 修复前后行为一致)."""

    def test_intraday_61min_low_peak_killed(self):
        meta = {"cohort": "A", "horizon": "short"}
        with patch("app.playbook.config.get_playbook_config", return_value=_CfgStub(CFG_BASE)), \
             patch("app.playbook.pipeline._is_overnight_position", return_value=False):
            reason, *_ = _check_playbook_exit_rules(
                direction="LONG", entry_price=100.0, current_price=100.1,
                initial_sl=95.0, peak_price=100.1,
                meta=meta, held_minutes=61.0, minutes_to_close=300.0,
                minutes_from_open=61.0, entry_time_str="2026-09-02T09:40:00-04:00",
            )
        assert reason == "DEAD_SIGNAL_EXIT"

    def test_intraday_59min_not_killed(self):
        meta = {"cohort": "A", "horizon": "short"}
        with patch("app.playbook.config.get_playbook_config", return_value=_CfgStub(CFG_BASE)), \
             patch("app.playbook.pipeline._is_overnight_position", return_value=False):
            reason, *_ = _check_playbook_exit_rules(
                direction="LONG", entry_price=100.0, current_price=100.1,
                initial_sl=95.0, peak_price=100.1,
                meta=meta, held_minutes=59.0, minutes_to_close=300.0,
                minutes_from_open=59.0, entry_time_str="2026-09-02T09:40:00-04:00",
            )
        assert reason != "DEAD_SIGNAL_EXIT"
