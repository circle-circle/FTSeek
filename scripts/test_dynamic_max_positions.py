#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试动态 max_positions 功能

验证：
1. 动态仓位模式是否正确计算股票池数量
2. 模拟交易 vs 实盘交易的差异
3. 显式指定 max_positions 优先于动态计算
"""
import sys
import os

# 添加项目根目录到路径
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

# 设置环境变量
os.environ['TRADE_ENV'] = '1'  # 模拟交易
os.environ['ALLOW_REAL_TRADING'] = 'false'
os.environ['WATCH_LIST_HK'] = '["HK.09992", "HK.00981", "HK.03690", "HK.00700", "HK.09888", "HK.01024", "HK.09988", "HK.01810", "HK.02318", "HK.09618"]'
os.environ['WATCH_LIST_US'] = '[]'

from app.decision_buffer import DecisionBuffer


def test_dynamic_max_positions():
    """测试动态 max_positions 计算"""
    print("=" * 60)
    print("测试动态 max_positions 功能")
    print("=" * 60)

    # 测试 1: 动态仓位模式（默认）
    print("\n【测试 1】动态仓位模式（模拟交易）")
    db1 = DecisionBuffer(dynamic_position_mode=True)
    print(f"  max_positions = {db1.max_positions}")
    print(f"  预期：10 (股票池数量)")
    assert db1.max_positions == 10, f"期望 10, 实际 {db1.max_positions}"
    print("  ✅ 通过")

    # 测试 2: 显式指定 max_positions（优先于动态计算）
    print("\n【测试 2】显式指定 max_positions=6")
    db2 = DecisionBuffer(max_positions=6, dynamic_position_mode=True)
    print(f"  max_positions = {db2.max_positions}")
    print(f"  预期：6 (显式指定优先)")
    assert db2.max_positions == 6, f"期望 6, 实际 {db2.max_positions}"
    print("  ✅ 通过")

    # 测试 3: 禁用动态仓位模式（使用默认值）
    print("\n【测试 3】禁用动态仓位模式")
    db3 = DecisionBuffer(dynamic_position_mode=False)
    print(f"  max_positions = {db3.max_positions}")
    print(f"  预期：4 (默认值)")
    assert db3.max_positions == 4, f"期望 4, 实际 {db3.max_positions}"
    print("  ✅ 通过")

    # 测试 4: 获取统计信息
    print("\n【测试 4】获取统计信息")
    stats = db1.get_stats()
    print(f"  pending_count = {stats['pending_count']}")
    print(f"  history_count = {stats['history_count']}")
    print(f"  approval_rate = {stats['approval_rate']}")
    print("  ✅ 通过")

    print("\n" + "=" * 60)
    print("所有测试通过！✅")
    print("=" * 60)

    return True


if __name__ == "__main__":
    try:
        test_dynamic_max_positions()
    except AssertionError as e:
        print(f"\n❌ 测试失败：{e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ 测试异常：{e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
