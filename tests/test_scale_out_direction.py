#!/usr/bin/env python3
"""Unit tests for direction×strategy-aware scale_out exit rules.

2026-06-20: 分层按 {direction}_{strategy}_atr_tiers 配置.
  数据驱动:
    SHORT|TECHNICAL_MULTI_FACTOR  t5=70%/+3.11% → 三层放长
    LONG|TECHNICAL_MULTI_FACTOR    t5=34%/-1.10% → 单层快平
    LONG|EARNINGS_REVISION_MOMENTUM t5 n=0      → 单层快平 (待补 t5)

关键回归点:
  - multi-tier 可达性 (is_partially_reduced 广域早退已移除, 依赖 triggered_tiers)
  - strategy key 优先于 direction key
  - 未配置 strategy 走方向级兜底
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from unittest.mock import MagicMock
from app.risk.exit_rules.scale_out import check_scale_out, _strategy_slug


def make_position(entry_price=100.0, size=100, is_reduced=False, strategy=""):
    """Create mock position object."""
    pos = MagicMock()
    pos.avg_entry_price = entry_price
    pos.size = size
    pos.symbol = "US.TEST"
    pos.is_partially_reduced = is_reduced
    # MagicMock auto-attrs return truthy MagicMock; pin strategy attrs explicitly
    pos.owner_strategy = strategy
    pos.strategy_id = strategy
    return pos


def md(price, atr, direction, strategy=""):
    return {"current_price": price, "atr": atr, "direction": direction, "strategy": strategy}


# ── strategy slug ──

def test_strategy_slug():
    assert _strategy_slug("TECHNICAL_MULTI_FACTOR") == "technical_multi_factor"
    assert _strategy_slug("EARNINGS_REVISION_MOMENTUM") == "earnings_revision_momentum"
    assert _strategy_slug("orb") == "orb"
    assert _strategy_slug("") == ""
    assert _strategy_slug(None) == ""           # None 安全
    assert _strategy_slug(MagicMock()) == ""    # MagicMock 安全 (非 str)
    print("  ✅ strategy slug 规范化")


# ── SHORT|TECHNICAL_MULTI_FACTOR: 三层放长 (multi-tier 可达性核心回归) ──

def test_short_technical_tier1():
    """SHORT|TECHNICAL 1.0ATR → 30% close + SL→entry(保本)."""
    pos = make_position(entry_price=100.0, strategy="TECHNICAL_MULTI_FACTOR")
    # SHORT entry=100, price=98, ATR=2 → profit=(100-98)/2=1.0 ATR
    result = check_scale_out(pos, 98.0, {}, "SHORT", market_data=md(98.0, 2.0, "SHORT", "TECHNICAL_MULTI_FACTOR"))
    assert result is not None
    assert result["close_ratio"] == 0.30, f"T1 close 30%, got {result['close_ratio']}"
    assert result["metadata"]["tier"] == 1
    assert result["stop_price"] == 100.0, f"SL→entry(100), got {result['stop_price']}"
    print("  ✅ SHORT|TECHNICAL T1 (1.0ATR): 30% close, SL=entry")


def test_short_technical_tier2_reachable():
    """回归: T2 必须可达 (旧 is_partially_reduced 守卫曾阻断).

    模拟 T1 已触发 (triggered_tiers=[0]), 价格到 2.0ATR → T2 触发.
    关键: is_partially_reduced=True 也不应阻断 (守卫已移除).
    """
    pos = make_position(entry_price=100.0, is_reduced=True, strategy="TECHNICAL_MULTI_FACTOR")
    # SHORT entry=100, price=96, ATR=2 → profit=(100-96)/2=2.0 ATR
    result = check_scale_out(pos, 96.0, {"triggered_tiers": [0]}, "SHORT",
                             market_data=md(96.0, 2.0, "SHORT", "TECHNICAL_MULTI_FACTOR"))
    assert result is not None, "T2 must be reachable despite is_partially_reduced=True"
    assert result["close_ratio"] == 0.30, f"T2 close 30%, got {result['close_ratio']}"
    assert result["metadata"]["tier"] == 2
    # SL = entry - 1.0*ATR = 100 - 2 = 98
    assert result["stop_price"] == 98.0, f"SL→entry-1ATR(98), got {result['stop_price']}"
    print("  ✅ SHORT|TECHNICAL T2 (2.0ATR): reachable, SL=entry-1ATR")


def test_short_technical_tier3_trailing():
    """T3: 0% close + trailing_remainder."""
    pos = make_position(entry_price=100.0, strategy="TECHNICAL_MULTI_FACTOR")
    # SHORT entry=100, price=94, ATR=2 → profit=3.0 ATR
    result = check_scale_out(pos, 94.0, {"triggered_tiers": [0, 1]}, "SHORT",
                             market_data=md(94.0, 2.0, "SHORT", "TECHNICAL_MULTI_FACTOR"))
    assert result is not None, "T3 must be reachable"
    assert result["close_qty"] == 0, "T3 no close"
    assert result["trailing_remainder"] is True
    assert result["metadata"]["tier"] == 3
    print("  ✅ SHORT|TECHNICAL T3 (3.0ATR): trailing remainder")


# ── LONG|TECHNICAL_MULTI_FACTOR: 快平 (不分层) ──

def test_long_technical_fast_close():
    """LONG|TECHNICAL 1.0ATR → 100% close (t5 负, 不放长)."""
    pos = make_position(entry_price=100.0, strategy="TECHNICAL_MULTI_FACTOR")
    result = check_scale_out(pos, 102.0, {}, "LONG",
                             market_data=md(102.0, 2.0, "LONG", "TECHNICAL_MULTI_FACTOR"))
    assert result is not None
    assert result["close_ratio"] == 1.0, f"LONG|TECHNICAL full close, got {result['close_ratio']}"
    assert result["metadata"]["tier"] == 1
    print("  ✅ LONG|TECHNICAL 1.0ATR: 100% close (fast)")


# ── LONG|EARNINGS_REVISION_MOMENTUM: 快平 (待 t5) ──

def test_long_earnings_fast_close():
    pos = make_position(entry_price=100.0, strategy="EARNINGS_REVISION_MOMENTUM")
    result = check_scale_out(pos, 102.0, {}, "LONG",
                             market_data=md(102.0, 2.0, "LONG", "EARNINGS_REVISION_MOMENTUM"))
    assert result is not None
    assert result["close_ratio"] == 1.0, f"EARNINGS full close, got {result['close_ratio']}"
    print("  ✅ LONG|EARNINGS 1.0ATR: 100% close (pending t5)")


# ── strategy key 解析优先级 ──

def test_strategy_key_overrides_direction():
    """SHORT|TECHNICAL (30% close) 优先于 SHORT 通用 (100% close)."""
    pos = make_position(entry_price=100.0, strategy="TECHNICAL_MULTI_FACTOR")
    result = check_scale_out(pos, 98.0, {}, "SHORT",
                             market_data=md(98.0, 2.0, "SHORT", "TECHNICAL_MULTI_FACTOR"))
    assert result["close_ratio"] == 0.30, "strategy-specific tier must win over direction tier"
    print("  ✅ strategy key 优先于 direction key")


def test_unknown_strategy_falls_back_to_direction():
    """未配置的 strategy → 方向级兜底 (100% close)."""
    pos = make_position(entry_price=100.0, strategy="SOME_NEW_STRATEGY")
    result = check_scale_out(pos, 98.0, {}, "SHORT",
                             market_data=md(98.0, 2.0, "SHORT", "SOME_NEW_STRATEGY"))
    assert result is not None
    assert result["close_ratio"] == 1.0, f"fallback full close, got {result['close_ratio']}"
    print("  ✅ unknown strategy → direction fallback (full close)")


def test_empty_direction_falls_back_to_atr_tiers():
    pos = make_position(entry_price=100.0, strategy="")
    result = check_scale_out(pos, 102.0, {}, "LONG", market_data=md(102.0, 2.0, "LONG", ""))
    assert result is not None
    assert result["close_ratio"] == 1.0
    print("  ✅ empty strategy → atr_tiers fallback")


# ── 防护 ──

def test_no_trigger_below_threshold():
    pos = make_position(entry_price=100.0, strategy="TECHNICAL_MULTI_FACTOR")
    result = check_scale_out(pos, 101.0, {}, "LONG", market_data=md(101.0, 2.0, "LONG", "TECHNICAL_MULTI_FACTOR"))
    assert result is None
    print("  ✅ No trigger below 1.0ATR")


def test_no_trigger_when_loss():
    pos = make_position(entry_price=100.0, strategy="TECHNICAL_MULTI_FACTOR")
    result = check_scale_out(pos, 98.0, {}, "LONG", market_data=md(98.0, 2.0, "LONG", "TECHNICAL_MULTI_FACTOR"))
    assert result is None
    print("  ✅ No trigger when losing")


if __name__ == "__main__":
    print("=== direction×strategy scale_out tests ===\n")
    tests = [
        test_strategy_slug,
        test_short_technical_tier1,
        test_short_technical_tier2_reachable,
        test_short_technical_tier3_trailing,
        test_long_technical_fast_close,
        test_long_earnings_fast_close,
        test_strategy_key_overrides_direction,
        test_unknown_strategy_falls_back_to_direction,
        test_empty_direction_falls_back_to_atr_tiers,
        test_no_trigger_below_threshold,
        test_no_trigger_when_loss,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t(); passed += 1
        except AssertionError as e:
            print(f"  ❌ {t.__name__}: {e}"); failed += 1
        except Exception as e:
            print(f"  ❌ {t.__name__}: {type(e).__name__}: {e}"); failed += 1
    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed == 0:
        print("✅ ALL TESTS PASSED")
    else:
        print(f"❌ {failed} TESTS FAILED"); sys.exit(1)
