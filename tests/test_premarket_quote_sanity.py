#!/usr/bin/env python3
"""RFC-177 盘前报价可信度判据 (record-only 先行) 的契约测试.

判据目标: 只挡"对持仓方向离谱地不利且盘口宽"的打印 (2026-10-20 ADSK 04:02 -56% 实锤),
不得挡真实顺势跳空 (那是兑现机会), 也不得因数据缺失而挡 (fail-open).
"""
import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load():
    p = ROOT / "scripts" / "premarket_exit_shadow.py"
    spec = importlib.util.spec_from_file_location("pm_shadow_quote_sane_test", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


M = _load()


def test_implausible_adverse_print_is_flagged():
    """ADSK 形态: 盘前打印 -56% 且盘口宽 → 判脏。"""
    bad, reason = M.quote_implausible_adverse(
        executable=128.0, prev_close=291.0, spread_pct=3.4, direction="LONG")
    assert bad is True
    assert "逆势偏离" in reason


def test_real_downside_gap_with_tight_book_is_not_flagged():
    """真实财报暴雷 -15% 但盘口窄 0.2% → 不是脏价, 必须放行(利润侧照常判定)。"""
    bad, reason = M.quote_implausible_adverse(
        executable=85.0, prev_close=100.0, spread_pct=0.2, direction="LONG")
    assert bad is False and reason == ""


def test_wide_book_without_extreme_deviation_is_not_flagged():
    """薄盘价差 2.5% 但逆向只 -1.2% → 不挡。"""
    bad, _ = M.quote_implausible_adverse(
        executable=98.8, prev_close=100.0, spread_pct=2.5, direction="LONG")
    assert bad is False


def test_favorable_spike_is_never_flagged():
    """+12% 顺势跳空 (兑现机会) 即使盘口宽也不挡 —— 挡它有害无益。"""
    bad, _ = M.quote_implausible_adverse(
        executable=112.0, prev_close=100.0, spread_pct=5.0, direction="LONG")
    assert bad is False


def test_short_side_direction_aware():
    """SHORT 的"不利"是价格上行: +14% 且盘口宽 → 脏; -14% 则是有利方向不挡。"""
    bad_up, _ = M.quote_implausible_adverse(
        executable=114.0, prev_close=100.0, spread_pct=3.0, direction="SHORT")
    bad_dn, _ = M.quote_implausible_adverse(
        executable=86.0, prev_close=100.0, spread_pct=3.0, direction="SHORT")
    assert bad_up is True
    assert bad_dn is False


def test_missing_inputs_fail_open():
    """任一输入缺失 → 不挡 (数据不全不是证据)。"""
    assert M.quote_implausible_adverse(None, 100.0, 3.0, "LONG")[0] is False
    assert M.quote_implausible_adverse(50.0, None, 3.0, "LONG")[0] is False
    assert M.quote_implausible_adverse(50.0, 100.0, None, "LONG")[0] is False
    assert M.quote_implausible_adverse(0.0, 100.0, 3.0, "LONG")[0] is False


def test_threshold_override_is_honoured():
    """阈值可注入 (测试/回放用), 且更宽阈值会放行同一打印。"""
    args = dict(executable=88.0, prev_close=100.0, spread_pct=2.5, direction="LONG")
    assert M.quote_implausible_adverse(**args)[0] is True          # 默认 10%/2%
    assert M.quote_implausible_adverse(**args, max_dev_pct=15.0)[0] is False
    assert M.quote_implausible_adverse(**args, max_spread_pct=3.0)[0] is False


def test_enforce_defaults_off():
    """record-only 是默认; 转硬必须显式设置 env。"""
    assert M.QUOTE_SANE_ENFORCE is False