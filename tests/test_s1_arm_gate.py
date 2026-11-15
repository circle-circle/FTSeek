"""S1 臂闸契约测试 (判据 B23 / 注册表 s1_short_after_open).

钉住五件:
  ① enabled=False 时全放行、不查库 (零行为变更语义);
  ② 方向: 只放行 SELL_SHORT (BUY/SHORT/LONG 全部拒);
  ③ 时段: ET<585 分钟拒 (黑窗虽有独立闸, 臂自身必须再守一次);
  ④ 帽: 单日笔数 / 在仓敞口 / 单笔缩股 (缩股不放大, 整股向下);
  ⑤ kill switch: 累计 < -300 或 n>=15 且净 bps<0 即拒 (S1/S2 两条停止条件)。
另钉 cohort 口径与判据字典 B23 一致 (n=88 / +30.75bps 的历史参照), 防谓词漂移。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.playbook import s1_arm as arm  # noqa: E402


def _st(enabled=True, **kw):
    base = {"enabled": enabled, "today": "2026-11-16", "today_count": 0,
            "exposure": 0.0, "killed": None, "cohort": {"n": 0, "net": 0.0}}
    base.update(kw)
    return base


# ① 关闸零行为变更
def test_disabled_arm_allows_everything():
    st = _st(enabled=False)
    assert arm.check_entry(st, direction="BUY", entry_minute=560) == (True, "")
    assert arm.cap_size(st, 1000, 10.0) == 1000


def test_disabled_cycle_state_skips_db():
    st = arm.cycle_state("2026-11-15", 0)
    assert st["enabled"] is False and st["exposure"] == 0.0 and st["killed"] is None


# ② 方向
@pytest.mark.parametrize("direction", ["BUY", "LONG", "SHORT", "", None])
def test_only_short_side_allowed(direction):
    ok, why = arm.check_entry(_st(), direction=direction, entry_minute=600)
    assert ok is False and why.startswith("s1_arm_direction")


def test_sell_short_allowed_in_window():
    ok, why = arm.check_entry(_st(), direction="SELL_SHORT", entry_minute=600)
    assert ok is True and why == ""


# ③ 时段
@pytest.mark.parametrize("minute", [570, 584, None])
def test_before_0945_rejected(minute):
    ok, why = arm.check_entry(_st(), direction="SELL_SHORT", entry_minute=minute)
    assert ok is False and why.startswith("s1_arm_before_open")


def test_exactly_0945_allowed():
    ok, _ = arm.check_entry(_st(), direction="SELL_SHORT", entry_minute=585)
    assert ok is True


# ④ 帽
def test_daily_count_cap():
    ok, why = arm.check_entry(_st(today_count=2), direction="SELL_SHORT", entry_minute=600)
    assert ok is False and why.startswith("s1_arm_daily_count")


def test_exposure_cap():
    ok, why = arm.check_entry(_st(exposure=8400.0), direction="SELL_SHORT", entry_minute=600)
    assert ok is False and why.startswith("s1_arm_exposure")


def test_cap_size_shrinks_never_grows():
    st = _st(exposure=0.0)
    assert arm.cap_size(st, 1000, 10.0) == 420          # 4200/10
    assert arm.cap_size(st, 100, 10.0) == 100           # 不放大
    assert arm.cap_size(_st(exposure=6000.0), 1000, 10.0) == 240   # 余量 2400/10


# ⑤ kill switch
def test_kill_switch_cum_loss():
    assert arm.kill_reason({"n": 3, "net": -300.0, "net_bps": -50.0}).startswith("S2_ECON")
    assert arm.kill_reason({"n": 3, "net": -299.9, "net_bps": -50.0}) is None


def test_kill_switch_direction_after_n():
    assert arm.kill_reason({"n": 15, "net": -5.0, "net_bps": -1.0}).startswith("S1_DIR")
    assert arm.kill_reason({"n": 14, "net": -5.0, "net_bps": -1.0}) is None


def test_killed_state_blocks_entry():
    ok, why = arm.check_entry(_st(killed="S2_ECON(cum_net=-350.00)"),
                              direction="SELL_SHORT", entry_minute=600)
    assert ok is False and why.startswith("s1_arm_killed")


# cohort 口径 (生产库, 只读) — 钉住判据字典 B23 的历史参照, 防谓词漂移
def test_cohort_matches_prereg_reference():
    h = arm.cohort_state("2026-07-01")
    assert h["n"] == 88, h
    assert abs(h["net"] - 1472.02) < 1.0, h
    assert abs(h["net_bps"] - 30.75) < 0.05, h


def test_forward_cohort_empty_before_start():
    assert arm.cohort_state("2026-11-16")["n"] == 0