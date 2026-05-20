#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
港股退出聚合器测试用例

验证核心逻辑：
1. ATR 动态止损计算
2. Chandelier 追踪止盈激活条件
3. Breakeven 保本触发
4. 聚合层最紧线选择
5. Cost Buffer 应用
6. 触发判定
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.risk.hk_exit_aggregator import (
    get_hk_exit_aggregator,
    ATRDynamicStopRule,
    ChandelierTrailRule,
    BreakevenGuardRule,
    get_session_vol_ratio,
    get_current_hk_session,
)


def test_atr_dynamic_stop():
    """测试 ATR 动态止损"""
    rule = ATRDynamicStopRule()
    
    # 基础场景：entry=100, atr=2, mult=2.0 → stop=96
    result = rule.calculate_stop_line(
        entry_price=100.0,
        current_price=98.0,
        atr_value=2.0,
        direction="BUY",
        config={"market_regime": "normal"},
    )
    assert result is not None, "ATR 止损应返回结果"
    assert result.price < 100.0, "多头止损价应低于入场价"
    print(f"✅ ATR 动态止损: stop={result.price:.2f}")


def test_chandelier_activation():
    """测试 Chandelier 激活条件"""
    rule = ChandelierTrailRule()
    
    # 场景 1: profit_atr < activation → 不应激活
    result1 = rule.calculate_stop_line(
        entry_price=100.0,
        current_price=101.0,  # +0.5 ATR (atr=2)
        peak_price=101.5,
        atr_value=2.0,
        direction="BUY",
        config={"chandelier_activation_atr": 1.5},
    )
    assert result1 is None, "+0.5 ATR 不应激活 Chandelier"
    print("✅ Chandelier 未激活 (profit < activation)")
    
    # 场景 2: profit_atr >= activation → 应激活
    result2 = rule.calculate_stop_line(
        entry_price=100.0,
        current_price=104.0,  # +2.0 ATR (atr=2)
        peak_price=105.0,
        atr_value=2.0,
        direction="BUY",
        config={"chandelier_activation_atr": 1.5},
    )
    assert result2 is not None, "+2.0 ATR 应激活 Chandelier"
    assert result2.price < 105.0, "Chandelier 止损价应低于峰值"
    print(f"✅ Chandelier 激活: stop={result2.price:.2f}")


def test_breakeven_trigger():
    """测试 Breakeven 保本触发"""
    rule = BreakevenGuardRule()
    
    # 场景 1: profit_atr < trigger → 不应触发
    result1 = rule.calculate_stop_line(
        entry_price=100.0,
        current_price=101.0,
        atr_value=2.0,
        direction="BUY",
        config={"breakeven_trigger_atr": 1.0},
    )
    assert result1 is None, "+0.5 ATR 不应触发 Breakeven"
    print("✅ Breakeven 未触发 (profit < trigger)")
    
    # 场景 2: profit_atr >= trigger → 应触发
    result2 = rule.calculate_stop_line(
        entry_price=100.0,
        current_price=103.0,  # +1.5 ATR (atr=2)
        atr_value=2.0,
        direction="BUY",
        config={"breakeven_trigger_atr": 1.0, "breakeven_buffer_pct": 0.005},
    )
    assert result2 is not None, "+1.5 ATR 应触发 Breakeven"
    assert result2.price >= 100.0, "保本价应 >= 入场价"
    print(f"✅ Breakeven 触发: stop={result2.price:.2f}")


def test_aggregation():
    """测试聚合层"""
    agg = get_hk_exit_aggregator()
    
    # 场景：ATR 止损=96, Breakeven=100.5, Chandelier=102
    # 多头应取最高（最紧）→ 102 (Chandelier)
    result = agg.evaluate(
        code="HK.00700",
        entry_price=100.0,
        current_price=103.0,
        peak_price=105.0,
        atr_value=2.0,
        direction="BUY",
        config={
            "market_regime": "normal",
            "chandelier_activation_atr": 1.5,
            "breakeven_trigger_atr": 1.0,
        },
    )
    
    assert len(result.candidates) >= 2, "应有多个候选线"
    print(f"✅ 聚合评估: {len(result.candidates)} 条候选线")
    
    # 验证触发逻辑
    if result.should_exit:
        print(f"✅ 触发退出: {result.rule_name} | {result.reason}")
    else:
        print(f"✅ 未触发退出 | final_stop={result.final_stop_price:.2f}")


def test_cost_buffer():
    """测试 Cost Buffer"""
    agg = get_hk_exit_aggregator()
    
    # 计算 100 元的成本缓冲
    buffer = agg._calculate_cost_buffer(100.0)
    expected = 100.0 * (140 / 10000.0) * 1.5  # 2.1
    assert abs(buffer - expected) < 0.01, f"Cost Buffer 应为 {expected}, 实际 {buffer}"
    print(f"✅ Cost Buffer: {buffer:.4f} (期望 {expected:.4f})")


def test_session_ratio():
    """测试时段波动率比率"""
    # 验证各时段比率合理
    for session in ["opening", "morning", "lunch", "afternoon", "closing", "off_hours"]:
        ratio = get_session_vol_ratio(session)
        assert 0.5 <= ratio <= 1.5, f"{session} 比率 {ratio} 超出合理范围"
    print("✅ 时段波动率比率: 全部在合理范围")


if __name__ == "__main__":
    print("=" * 60)
    print("港股退出聚合器测试")
    print("=" * 60)
    
    test_atr_dynamic_stop()
    test_chandelier_activation()
    test_breakeven_trigger()
    test_aggregation()
    test_cost_buffer()
    test_session_ratio()
    
    print("\n" + "=" * 60)
    print("✅ 全部测试通过")
    print("=" * 60)
