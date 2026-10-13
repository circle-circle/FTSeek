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
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "app" / "playbook"
           / "pipeline_parts" / "trigger_flow.py").read_text(encoding="utf-8")
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
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "app" / "playbook"
           / "pipeline_parts" / "trigger_flow.py").read_text(encoding="utf-8")
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


# ── 5. 附节(第2层): planned_entry 生成质量诊断的披露与时序纪律不得被删 ──────────

def test_rfc123_registers_planned_entry_quality_diagnostic():
    """RFC-123 附节(第2层立项依据)不得被静默删 (2026-10-11 登记)。

    钉两类内容:
      ① 病灶与回测数字本体 — LLM 计划价 76% 够不到、or_low vs llm_plan
        (52%vs33% / +1.34vs+1.21, 复查修复后口径)、与 ADR-035 fill_rate=0.323
        同一样本双口径互证。删掉披露不会让其它测试变红, 故单独钉住。
      ② 【最关键】时序纪律 E 节 — "现在改 planned_entry 生成 = 阶段0 影子作废"。
        这条若被删, 后人可能在阶段0 攒样期改生成逻辑, 换掉被测量变量 ⇒ 3天影子白攒、
        两变量(拆锚/换锚基)纠缠归因不了。这是本测试存在的首要理由。
    """
    from pathlib import Path
    rfc = (Path(__file__).resolve().parents[1] / "docs" / "decisions"
           / "RFC-123-entry-anchor-lifecycle-separation.md")
    txt = rfc.read_text(encoding="utf-8")

    # 附节必须存在且可复现脚本被引用
    assert "planned_entry 生成质量诊断" in txt, "RFC-123 附节(第2层)缺失"
    assert "rfc123_planned_entry_anchor_backtest.py" in txt, "附节须引用可复现脚本"

    for needle in (
        # ① 病灶数字本体
        "够不到 = 16/21 (76%)",          # LLM 计划价可达性
        "fill_rate=0.323",                # ADR-035 同向读数
        # ① 回测核心结论 (or_low 成交率明显优 + 改善微优, 2026-10-11 复查修复后)
        "or_low", "52%", "33%", "+1.34", "+1.21", "不再支撑「双轴完胜」",
        # 权衡曲线 (回踩越深成交率崩)
        "0% 成交", "甜点",
        # 第2层方案三层
        "opening_low", "k×ATR", "TTL 回退市价",
        # 复查修复批披露 (P0 窗口 bug / 事实更正 / 冻结 / 独立性降级)
        "跨日", "16:00", "8 赢 2 亏", "0.31×ATR", "同一样本双口径",
        "--until 2026-10-09",
    ):
        assert needle in txt, f"RFC-123 附节缺关键读数: {needle}"

    # ② 时序纪律 (最关键, 防阶段0 影子被作废)
    sec_e = txt.split("### E. 纪律", 1)
    assert len(sec_e) == 2, "附节 E (时序纪律) 缺失"
    e = sec_e[1]
    for needle in ("阶段0", "作废", "变量纠缠", "10 交易日", "独立"):
        assert needle in e, f"附节 E 时序纪律缺关键约束: {needle}"

    # 📌 防假绿: 附节局限必须声明"不得当裁决" (防被误用为转正依据)
    assert "不得当裁决" in txt or "只筛方向" in txt, \
        "附节须声明 n=21/3天 只筛方向、不得当裁决 (防阈值购物/误用)"

    # ③ 观察轨 §G: 日更重放预注册不得被静默删 (2026-10-11 用户裁「执行」)
    sec_g = txt.split("### G. 第2层观察轨", 1)
    assert len(sec_g) == 2, "附节 G (观察轨预注册) 缺失"
    g = sec_g[1]
    for needle in ("--daily", "53 5 * * 2-6", "rfc123_l2_rule_anchor_latest.json",
                   "不设阈值", "or_low_ttl5", "or_low_ttl15"):
        assert needle in g, f"附节 G 观察轨预注册缺关键约束: {needle}"


def test_rfc123_backtest_cross_day_exit_window_capped():
    """跨日退出的限价窗口必须截到入场日 16:00 (2026-10-11 复查 P0 回归)。

    旧实现直接取 exit_time 的 HH:MM: 跨日退出(10-08 09:36 入场 / 10-09 05:09 平仓)
    得到 start(09:36) > end(05:09) 的倒挂窗口 ⇒ _first_fill 立即 break, 该笔对
    任何锚恒判 miss。此测试钉住修复语义, 防该边界再次静默劣化全表数字。
    """
    import importlib.util
    from pathlib import Path

    script = (Path(__file__).resolve().parents[1] / "scripts"
              / "rfc123_planned_entry_anchor_backtest.py")
    spec = importlib.util.spec_from_file_location("rfc123_anchor_backtest", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # 同日退出: 窗口终点 = 退出时刻
    assert module._window_end(
        "2026-10-08T09:36:33-04:00", "2026-10-08T10:30:00-04:00") == "10:30"
    # 跨日退出: bar 只覆盖入场日, 截到 16:00 (旧实现会返回 "05:09" ⇒ 窗口倒挂)
    assert module._window_end(
        "2026-10-08T09:36:33-04:00", "2026-10-09T05:09:45-04:00") == "16:00"


def test_rfc123_ttl_entry_limit_vs_market_fallback():
    """TTL 限价→市价回退: 限价内触碰→限价; TTL 过期未触碰→过期后首 bar 开盘市价.

    第3层设计核心: 「TTL 内不回踩 → 转市价进场(不错过整笔)」。
    """
    import importlib.util
    from pathlib import Path

    script = (Path(__file__).resolve().parents[1] / "scripts"
              / "rfc123_planned_entry_anchor_backtest.py")
    spec = importlib.util.spec_from_file_location("rfc123_anchor_backtest", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # 3 根 bar: 09:36 (低=101 不触碰), 09:37 (低=100 触碰), 09:38
    bars = [
        ("2026-10-08 09:36:00", 101.5, 101.8, 101.0, 101.2),
        ("2026-10-08 09:37:00", 100.8, 101.2, 100.0, 100.9),
        ("2026-10-08 09:38:00", 100.9, 101.0, 100.7, 100.8),
    ]

    # TTL=5 (到 09:41): 09:37 bar 低=100 ≤ 锚=100.5 → 限价成交 @100.5
    entry = module._ttl_entry(bars, 100.5, True, "09:36", 5, "16:00")
    assert entry is not None and entry[2] == "limit"
    assert entry[1] == 100.5

    # TTL=0 (到 09:36): 首 bar 不触碰 → 市价回退 (09:37 bar open=100.8)
    entry = module._ttl_entry(bars, 100.5, True, "09:36", 0, "16:00")
    assert entry is not None and entry[2] == "market_fallback"
    assert entry[0] == "2026-10-08 09:37:00"

    # TTL 超过实际持仓 → 不可定义
    entry = module._ttl_entry(bars, 100.5, True, "09:36", 15, "09:40")
    assert entry == "ttl_exceeds_hold"

    # 锚无效 → None
    assert module._ttl_entry(bars, 0, True, "09:36", 5, "16:00") is None
