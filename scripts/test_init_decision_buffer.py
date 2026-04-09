#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试 init_decision_buffer 从配置中心读取配置

验证:
1. 配置中心读取 default 值
2. 动态计算 max_positions
3. 配置覆盖功能
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

from app.decision_buffer import init_decision_buffer, get_decision_buffer, _decision_buffer


def test_init_from_config():
    """测试从配置中心初始化"""
    print("=" * 60)
    print("测试 init_decision_buffer 从配置中心读取")
    print("=" * 60)

    # 重置全局单例
    global _decision_buffer
    from app import decision_buffer
    decision_buffer._decision_buffer = None

    # 测试 1: 从配置中心读取 (配置文件中 max_positions=null)
    print("\n【测试 1】从配置中心初始化 (max_positions=null)")
    db = init_decision_buffer()
    print(f"  max_positions = {db.max_positions}")
    print(f"  预期：10 (股票池数量，动态计算)")
    assert db.max_positions == 10, f"期望 10, 实际 {db.max_positions}"
    print("  ✅ 通过")

    # 测试 2: 获取全局单例
    print("\n【测试 2】获取全局单例")
    db2 = get_decision_buffer()
    print(f"  max_positions = {db2.max_positions}")
    assert db2 is db, "应该是同一个实例"
    print("  ✅ 通过")

    # 测试 3: 显式覆盖配置
    print("\n【测试 3】显式覆盖配置 (max_positions=5)")
    decision_buffer._decision_buffer = None
    db3 = init_decision_buffer(max_positions=5, dynamic_position_mode=False)
    print(f"  max_positions = {db3.max_positions}")
    print(f"  预期：5 (显式指定)")
    assert db3.max_positions == 5, f"期望 5, 实际 {db3.max_positions}"
    print("  ✅ 通过")

    print("\n" + "=" * 60)
    print("所有测试通过！✅")
    print("=" * 60)

    return True


if __name__ == "__main__":
    try:
        test_init_from_config()
    except AssertionError as e:
        print(f"\n❌ 测试失败：{e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ 测试异常：{e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
