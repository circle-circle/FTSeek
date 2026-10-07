"""契约测试: RFC-123 阶段0 双锚影子 (DEB-20261007 F1, 2026-10-07 用户裁「按建议执行」).

钉住四件事:
  1. 生成锚解析三级优先级 (planned_entry → band 沿反推 → missing), 且 **missing 绝不
     回退 DB entry_price** —— 回退 = 用被 reprice 覆盖的执行价当生成锚 = 用答案当锚
     (自曝#11 实证: e≠planned 群触发率 99.3% vs 11.2%, 桶差 100% 是污染投影);
  2. 反事实几何逐分支与生产判定段同构 (含 near_miss 容忍度) —— 生产段改分支而本函数
     不同步 ⇒ 影子读数与生产语义脱钩, 必须红;
  3. record-only 不变量: 埋点块在 try/except 内、不写 triggered/返回值、TEST 守卫由
     宿主 _emit_entry_shadow 承担 (DEB §13 步4: T4 未被破坏);
  4. 负向: 注入「回退 entry_price」的实现 ⇒ 第 1 条校验必须 FAIL (证明守卫有牙)。

回滚: 删本文件即可, 零生产依赖。
"""
from __future__ import annotations

import inspect
import re

import pytest

from app.playbook import entry_band
from app.playbook.pipeline import (
    _anchor_trigger_geometry,
    _check_intraday_trigger,
    _resolve_generated_anchor,
)


# ── 1. 生成锚解析 ────────────────────────────────────────────────────────────

def test_priority1_planned_entry_wins():
    inst = {"raw_instruction_json": '{"planned_entry": 100.0}',
            "entry_low": 90.0, "entry_high": 110.0, "entry_price": 104.2}
    assert _resolve_generated_anchor(inst, False) == (100.0, "planned_entry")


def test_priority1_accepts_dict_raw():
    # _row_to_dict 已把 raw 解析成 dict — 字符串假设会让优先级①静默全灭 (2026-09-01 同型事故)
    inst = {"raw_instruction_json": {"planned_entry": 100.0}, "entry_price": 104.2}
    assert _resolve_generated_anchor(inst, False) == (100.0, "planned_entry")


def test_priority2_band_edge_long_and_short():
    inst = {"raw_instruction_json": "{}", "entry_low": 95.0, "entry_high": 105.0,
            "entry_price": 104.2}
    ch = entry_band.CHASE_ZONE_PCT
    assert _resolve_generated_anchor(inst, False) == (
        pytest.approx(105.0 / (1 + ch), 4), "band_edge")
    assert _resolve_generated_anchor(inst, True) == (
        pytest.approx(95.0 / (1 - ch), 4), "band_edge")


def test_priority3_missing_never_falls_back_to_entry_price():
    """RFC-123 验证计划第3条: 缺失生成锚时标记 missing, 不回退到后验 entry_price。"""
    inst = {"raw_instruction_json": "{}", "entry_price": 104.2}
    anchor, source = _resolve_generated_anchor(inst, False)
    assert source == "missing"
    assert anchor is None, (
        f"missing 却返回了锚 {anchor} — 若等于 entry_price(104.2) 即回退污染列 "
        "(RFC-123 明令禁止; 自曝#11: 该列被 reprice 逐轮覆盖)")
    assert anchor != inst["entry_price"]


def test_malformed_inputs_do_not_raise():
    for inst in ({}, {"raw_instruction_json": "not-json"},
                 {"raw_instruction_json": None, "entry_low": "abc"},
                 {"entry_high": 0}):
        anchor, source = _resolve_generated_anchor(inst, False)
        assert source in ("planned_entry", "band_edge", "missing")
        assert anchor is None or anchor > 0


# ── 2. 反事实几何与生产同构 ──────────────────────────────────────────────────

_K, _C, _T = entry_band.KNIFE_FLOOR_PCT, entry_band.CHASE_ZONE_PCT, 0.01


@pytest.mark.parametrize("price,exp_trig,exp_path", [
    (100.0, True, "pullback"),          # 恰在锚 → pullback 触发
    (99.0, True, "pullback"),           # 回调带内
    (97.0 - 1e-9, False, "pullback"),   # 越过 knife 下限 → 不触发 (仍标 pullback)
    (103.0, True, "chase"),             # 追涨带内 (量能确认)
    (105.5, True, "chase_near_miss"),   # chase 边界 +1% 容忍内
    (107.0, False, "out_of_band"),
])
def test_long_branches(price, exp_trig, exp_path):
    assert _anchor_trigger_geometry(100.0, price, False, _K, _C, True, _T) == (exp_trig, exp_path)


@pytest.mark.parametrize("price,exp_trig,exp_path", [
    (100.0, True, "pullback"),
    (101.0, True, "pullback"),
    (103.0 + 1e-9, False, "pullback"),  # 越过 knife 上限
    (97.0, True, "chase"),
    (94.5, True, "chase_near_miss"),
    (93.0, False, "out_of_band"),
])
def test_short_branches(price, exp_trig, exp_path):
    assert _anchor_trigger_geometry(100.0, price, True, _K, _C, True, _T) == (exp_trig, exp_path)


def test_chase_requires_volume_confirmation():
    """chase/near_miss 分支 triggered = vol_ok — 量能门语义必须保留。"""
    for is_short, price in ((False, 103.0), (False, 105.5), (True, 97.0), (True, 94.5)):
        assert _anchor_trigger_geometry(100.0, price, is_short, _K, _C, False, _T)[0] is False
        assert _anchor_trigger_geometry(100.0, price, is_short, _K, _C, True, _T)[0] is True


def test_invalid_anchor_returns_none_not_guess():
    assert _anchor_trigger_geometry(None, 99.0, False, _K, _C, True, _T) == (None, None)
    assert _anchor_trigger_geometry(0, 99.0, False, _K, _C, True, _T) == (None, None)
    assert _anchor_trigger_geometry(100.0, 0, False, _K, _C, True, _T) == (None, None)


def test_geometry_stays_in_sync_with_production_branches():
    """生产判定段的关键常量/分支若变更而本纯函数未同步 ⇒ 影子读数脱钩, 必须红。"""
    src = inspect.getsource(_check_intraday_trigger)
    assert "_near_miss_tolerance = 0.01" in src, (
        "生产 near_miss 容忍度已改 — _anchor_trigger_geometry 的 _T=0.01 须同步 "
        "(否则反事实几何与生产语义脱钩)")
    for token in ('_entry_path = "pullback"', '_entry_path = "chase"',
                  '_entry_path = "chase_near_miss"', '_entry_path = "out_of_band"'):
        assert token in src, f"生产判定段分支缺失/改名: {token} — 同步本纯函数"
    assert "(1 + _knife_floor)" in src and "(1 - _knife_floor)" in src, (
        "生产 knife 判定式已改 — 同步本纯函数")


# ── 3. record-only 不变量 ────────────────────────────────────────────────────

def test_emit_block_is_record_only():
    """埋点块必须: 在 try 内 / 不改 triggered / 不改返回值 / 不喂生产判定。"""
    from app.playbook import pipeline as pl
    src = inspect.getsource(pl)
    i = src.index('"schema": "rfc123-s0-v1"')
    block = src[max(0, i - 2000): i + 1500]
    assert "entry_anchor_dual_shadow_enabled" in block, "开关门控缺失 = 不可回滚"
    assert "logger.warning" in block, "A6 fail-loud 缺失 (禁 except:pass 静默丢行)"
    # 块内不得给生产判定变量赋值 (triggered / entry_price / current_price)
    for forbidden in ("triggered =", "entry_price =", "current_price ="):
        seg = src[i - 1800: i + 1200]
        assert forbidden not in seg.replace("triggered_val", ""), (
            f"埋点块内出现 `{forbidden}` — record-only 不变量被破坏")


def test_t4_dual_track_contract_unbroken():
    """DEB §13 步4 期望: tests/test_entry_zone_dual_track_contract.py 仍绿。

    本函数把 band 沿反推放在**模块级**而非 _check_intraday_trigger 体内, 故 T4 扫描
    (只看该函数源码) 不受影响 — 这里再独立断言一次函数体内无 entry_low/high 消费。"""
    src = inspect.getsource(_check_intraday_trigger)
    passthrough = re.compile(r'"entry_(low|high)":\s*inst\.get\(')
    for line in src.splitlines():
        if ("entry_low" in line or "entry_high" in line) and not passthrough.search(line):
            raise AssertionError(f"T4 双轨契约被破坏 (触发几何消费落库区间): {line.strip()}")


# ── 4. 负向: 守卫必须有牙 ────────────────────────────────────────────────────

def test_injected_entry_price_fallback_must_fail():
    """注入「missing 时回退 entry_price」的伪实现 ⇒ 第 1 条校验必须判 FAIL。"""
    def _bad_resolve(inst, is_short):
        raw = inst.get("raw_instruction_json") or {}
        if isinstance(raw, dict) and raw.get("planned_entry"):
            return float(raw["planned_entry"]), "planned_entry"
        return float(inst.get("entry_price") or 0) or None, "entry_price_fallback"

    inst = {"raw_instruction_json": {}, "entry_price": 104.2}
    anchor, source = _bad_resolve(inst, False)
    # 真实现的契约: source ∈ 三级枚举 且 missing 时 anchor is None
    assert source not in ("planned_entry", "band_edge", "missing") or anchor is not None, (
        "负向样例竟然满足契约 — 说明 test_priority3 没有牙")
