"""RFC-188 分段落袋线影子 — 纯函数不变式 + 落盘门控.

钉住四条不变量 (改实现前先看这里):
 ① S0/S1/S2 分段的边界连续、且 S2 的锁利地板不被 dd 吃掉;
 ② 任何阶段返回的 SL **永不差于初始 SL**(ratchet 地板);
 ③ peak − dd ≤ 0 时**不武装**(锁到保本线等于现价平仓 = 反效果);
 ④ 影子只写盘: 开关关 → 不写; 去重窗口内 → 不写; 写失败不抛。
"""
from __future__ import annotations

import json
import os

import pytest

from app.playbook import exit_staging_shadow as stg

ENTRY = 100.0
INIT_LONG = 95.0
INIT_SHORT = 105.0


def _cfg(enabled=True, **kw):
    base = {"enabled": enabled, "arm_hi_pct": 0.8, "lock_pct": 0.4,
            "dd_pct": 0.4, "dd_min_peak_pct": 0.3, "min_interval_s": 300}
    base.update(kw)
    return base


@pytest.fixture
def cfg_on(monkeypatch):
    monkeypatch.setattr(stg, "get_config", lambda: _cfg(True))


@pytest.fixture
def cfg_off(monkeypatch):
    monkeypatch.setattr(stg, "get_config", lambda: _cfg(False))


# ── ① 分段边界 ──
def test_s0_below_dd_min():
    sl, stage = stg.staged_sl("LONG", ENTRY, INIT_LONG, 0.29)
    assert stage == "S0" and sl == INIT_LONG


def test_s1_proportional_lock():
    # peak 0.6, dd 0.4 → 锁 +0.2%
    sl, stage = stg.staged_sl("LONG", ENTRY, INIT_LONG, 0.6)
    assert stage == "S1" and sl == pytest.approx(100.2, abs=1e-6)


def test_s2_lock_floor_wins_when_dd_eats_it():
    # peak 0.8 = arm_hi, dd 0.8 → peak-dd=0, 但 S2 地板 lock=0.4 ⇒ +0.4%
    sl, stage = stg.staged_sl("LONG", ENTRY, INIT_LONG, 0.8, dd_pct=0.8)
    assert stage == "S2" and sl == pytest.approx(100.4, abs=1e-6)


def test_s2_continues_to_ratchet_above_lock():
    # peak 1.5, dd 0.4 → +1.1% (高于 lock 地板, 应跟随峰值)
    sl, stage = stg.staged_sl("LONG", ENTRY, INIT_LONG, 1.5)
    assert stage == "S2" and sl == pytest.approx(101.1, abs=1e-6)


def test_boundary_continuity_at_arm_hi():
    lo, _ = stg.staged_sl("LONG", ENTRY, INIT_LONG, 0.7999)
    hi, _ = stg.staged_sl("LONG", ENTRY, INIT_LONG, 0.8)
    assert abs(hi - lo) < 1e-3


# ── ② ratchet 地板 ──
def test_never_worse_than_initial_long():
    for peak in (0.0, 0.3, 0.35, 0.4, 0.6, 0.8, 1.2, 3.0):
        sl, _ = stg.staged_sl("LONG", ENTRY, INIT_LONG, peak)
        assert sl >= INIT_LONG - 1e-9, peak


def test_never_worse_than_initial_short():
    for peak in (0.0, 0.3, 0.4, 0.6, 0.8, 1.2, 3.0):
        sl, _ = stg.staged_sl("SHORT", ENTRY, INIT_SHORT, peak)
        assert sl <= INIT_SHORT + 1e-9, peak


# ── ③ 锁到 0 以下 ⇒ 不武装 ──
def test_no_arm_when_lock_would_be_zero_or_negative():
    # peak 0.35, dd 0.4 → pct<0 ⇒ 不武装 (否则等于按现价平仓)
    sl, stage = stg.staged_sl("LONG", ENTRY, INIT_LONG, 0.35)
    assert stage == "S0" and sl == INIT_LONG


def test_would_exit_direction_aware():
    assert stg.would_exit("LONG", 99.0, 100.0) is True
    assert stg.would_exit("LONG", 101.0, 100.0) is False
    assert stg.would_exit("SHORT", 101.0, 100.0) is True
    assert stg.would_exit("SHORT", 99.0, 100.0) is False


# ── ④ 落盘门控 ──
def test_no_write_when_disabled(cfg_off, tmp_path):
    p = tmp_path / "s.jsonl"
    assert stg.record("LONG", "US.X", ENTRY, 100.5, 0.6, INIT_LONG, INIT_LONG, 12,
                      meta={}, path=str(p)) is None
    assert not p.exists()


def test_write_and_dedupe(cfg_on, tmp_path):
    p = tmp_path / "s.jsonl"
    meta = {}
    r1 = stg.record("LONG", "US.X", ENTRY, 100.5, 0.6, INIT_LONG, INIT_LONG, 12,
                    meta=meta, path=str(p))
    assert r1 and r1["stage"] == "S1" and r1["staged_sl"] == pytest.approx(100.2)
    # 同阶段 + 窗口内 → 不写
    assert stg.record("LONG", "US.X", ENTRY, 100.4, 0.62, INIT_LONG, INIT_LONG, 12.1,
                      meta=meta, path=str(p)) is None
    # 阶段变化 → 写
    r2 = stg.record("LONG", "US.X", ENTRY, 101.0, 0.9, INIT_LONG, INIT_LONG, 20,
                    meta=meta, path=str(p))
    assert r2 and r2["stage"] == "S2"
    rows = [json.loads(l) for l in open(p) if l.strip()]
    assert len(rows) == 2 and rows[1]["peak_pct"] == pytest.approx(0.9)


def test_survives_bad_input(cfg_on, tmp_path):
    p = tmp_path / "s.jsonl"
    # 非法参数不得抛 (影子失败不能影响退出链)
    assert stg.record("LONG", "US.X", None, None, None, None, None, 0,
                      meta={}, path=str(p)) is None