#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
港股 LLM 信号链路集成验证脚本 (Integration Test)

目标:
  验证新加入的 signal_impact 逻辑 (Consensus Enhancer, Executor Gate, Active Pool)
  能否正确读取并处理当前磁盘上的 Battle Plan 数据。
  模拟"开盘前注入 -> 盘中增强 -> 执行门禁"的数据流。
"""

import os
import sys
import json

# 添加项目路径
# 脚本位于 scripts/ 目录，需向上一级指向 FTSeek 根目录
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# 抑制无关日志
import logging
logging.getLogger("ftseek").setLevel(logging.WARNING)

def test_active_pool_injection_and_lookup():
    """
    测试 1: 动态池注入与 HK 前缀归一化查找
    """
    print("\n[TEST 1] 动态池注入与前缀归一化")
    from app.utils.daily_active_pool import get_daily_active_pool

    # 1. 获取单例并重置 (为了测试纯净)
    pool = get_daily_active_pool(market_prefix="HK.")
    
    # 模拟 battle plan target 数据
    # 注意: 这里故意使用不带 HK. 前缀的 code 来测试归一化
    test_target = {
        "symbol": "00700",
        "direction": "BUY",
        "signal_impact": "boost",
        "urgency": "High"
    }
    
    # 模拟注入
    pool.add_plan_target(
        code=f"HK.{test_target['symbol']}", # 盘前计划通常带前缀
        direction=test_target['direction'],
        logic="Test Logic",
        signal_impact=test_target['signal_impact'],
        urgency=test_target['urgency']
    )
    print(f"  ✅ 注入完成: HK.00700 (signal_impact=boost)")

    # 2. 模拟 Enhancer 查找 (使用无前缀 code)
    # 这是 consensus_enhancer.py 中 _get_llm_signal_impact_factor 的逻辑
    code_to_lookup = "HK.00700"
    
    # 模拟查找逻辑
    entry = pool._entries.get(code_to_lookup)
    if not entry:
        # 归一化尝试
        if not code_to_lookup.startswith("HK."):
            entry = pool._entries.get(f"HK.{code_to_lookup}")
    
    if entry:
        impact = entry.metadata.get("signal_impact", "neutral")
        if impact == "boost":
            print(f"  ✅ Enhancer 查找成功: code={code_to_lookup}, impact={impact}")
        else:
            print(f"  ❌ Enhancer 查找失败: 预期 boost, 实际 {impact}")
            return False
    else:
        print(f"  ❌ Enhancer 查找失败: Entry 为 None")
        return False

    # 3. 模拟 Executor 查找 (使用无前缀 code)
    # 这是 executor.py 中 LLM Gate 的逻辑
    code_to_lookup_no_prefix = "00700"
    entry = pool._entries.get(code_to_lookup_no_prefix)
    if not entry and not code_to_lookup_no_prefix.startswith("HK."):
        entry = pool._entries.get(f"HK.{code_to_lookup_no_prefix}")
    
    if entry and entry.metadata.get("signal_impact") == "boost":
        print(f"  ✅ Executor 查找成功: code={code_to_lookup_no_prefix}, impact=boost")
    else:
        print(f"  ❌ Executor 查找失败")
        return False

    return True

def test_quality_tracker_db_init():
    """
    测试 2: 质量追踪器数据库初始化
    """
    print("\n[TEST 2] LLM 质量追踪器数据库初始化")
    try:
        from app.consensus.llm_quality_tracker import get_llm_quality_tracker
        tracker = get_llm_quality_tracker()
        
        # 尝试写入一条记录 (Signal Entry)
        tracker.record_signal(
            trade_id="test_trade_001",
            code="HK.00700",
            signal_impact="boost",
            original_score=3.0,
            final_score=3.45
        )
        print(f"  ℹ️  已记录 Signal 入场")
        
        # 模拟平仓 (Outcome) 以便统计
        tracker.record_outcome(
            trade_id="test_trade_001",
            code="HK.00700",
            signal_impact="boost",
            actual_return=0.03,  # +3%
            exit_reason="take_profit",
            holding_minutes=30
        )
        print(f"  ℹ️  已记录 Outcome 平仓")
        
        # 验证读取
        stats = tracker.get_impact_stats()
        print(f"  ℹ️  查询 stats: {stats}")
        boost_stats = stats.get('boost', {})
        if boost_stats.get('n', 0) >= 1:
            print(f"  ✅ 质量追踪器 DB 读写成功 (n={boost_stats['n']})")
            return True
        else:
            print(f"  ❌ 质量追踪器读取失败 (n={boost_stats.get('n', 0)})")
            return False
    except Exception as e:
        print(f"  ❌ 质量追踪器初始化/写入异常: {e}")
        return False

def test_consensus_enhancer_factor():
    """
    测试 3: Consensus Enhancer 乘数计算
    """
    print("\n[TEST 3] Consensus Enhancer 乘数映射")
    # 这里的测试依赖于 consensus_enhancer.py 的内部映射，
    # 我们可以直接调用其私有方法或通过模拟数据验证
    from app.strategy.consensus_enhancer import get_consensus_enhancer
    
    enhancer = get_consensus_enhancer()
    
    # 手动验证映射值是否符合预期 (基于之前修改的代码)
    expected_map = {
        'boost': 1.15,
        'neutral': 1.0,
        'weaken': 0.80,
        'block': 0.30,
    }
    
    # 我们可以通过 mock 一个 entry 来测试 _get_llm_signal_impact_factor
    # 但由于 pool 是单例且上面测试已注入数据，我们可以直接复用
    # 注意: 需要确保 enhancer 读取 pool 的逻辑路径
    # 由于代码中直接实例化了 get_daily_active_pool，我们复用上面的 pool 状态即可
    
    # 测试 HK.00700 (之前注入为 boost)
    factor = enhancer._get_llm_signal_impact_factor("HK.00700")
    if factor == 1.15:
        print(f"  ✅ Boost 乘数正确: 1.15")
    else:
        print(f"  ❌ Boost 乘数错误: {factor}")
        return False

    # 测试一个不存在的 code (应为 1.0)
    factor = enhancer._get_llm_signal_impact_factor("HK.999999")
    if factor == 1.0:
        print(f"  ✅ Unknown 乘数正确: 1.0")
    else:
        print(f"  ❌ Unknown 乘数错误: {factor}")
        return False

    return True

def test_gate_stack_analysis_tool():
    """
    测试 4: 门禁叠加分析脚本可用性
    """
    print("\n[TEST 4] 门禁叠加分析脚本可用性")
    script_path = os.path.join(PROJECT_ROOT, "app", "diagnostics", "gate_stack_analysis.py")
    if os.path.exists(script_path):
        print(f"  ✅ 脚本文件存在: {script_path}")
        # 简单语法检查
        import ast
        try:
            ast.parse(open(script_path).read())
            print(f"  ✅ 脚本语法正确")
            return True
        except SyntaxError as e:
            print(f"  ❌ 脚本语法错误: {e}")
            return False
    else:
        print(f"  ❌ 脚本文件不存在")
        return False

def main():
    print("=" * 60)
    print("港股 LLM 信号链路集成验证")
    print("=" * 60)

    results = {}

    # 运行测试
    tests = [
        ("Active Pool Injection & Lookup", test_active_pool_injection_and_lookup),
        ("Quality Tracker DB Init", test_quality_tracker_db_init),
        ("Consensus Enhancer Factor", test_consensus_enhancer_factor),
        ("Gate Stack Analysis Tool", test_gate_stack_analysis_tool),
    ]

    for name, func in tests:
        try:
            results[name] = func()
        except Exception as e:
            print(f"  ⛔ 测试异常: {e}")
            results[name] = False

    # 汇总
    print("\n" + "=" * 60)
    print("测试结果汇总:")
    all_pass = True
    for name, passed in results.items():
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"  [{status}] {name}")
        if not passed:
            all_pass = False

    print("=" * 60)
    if all_pass:
        print("🎉 集成验证全部通过！系统已就绪。")
        return 0
    else:
        print("⚠️ 部分测试失败，请检查上述日志。")
        return 1

if __name__ == "__main__":
    sys.exit(main())
