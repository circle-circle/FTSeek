#!/usr/bin/env python3
"""
测试脚本: 验证模拟盘完整订单状态机链路

目标: 确认 simulate_realistic_flow=true + simulate_fill_delay_seconds=0.5 时
订单状态正确经历: CREATED → VALIDATED → SENT → ACKED → FILLED
"""

import sys
import time
import threading
from datetime import datetime

sys.path.insert(0, '/opt/ftseek/FTSeek-US')

from app.execution.engine import ExecutionEngine
from app.core.events import EventStore
from app.core.models import Order, OrderState, Direction, OrderType


def test_full_order_state_flow():
    """测试完整订单状态机链路"""
    print("=" * 70)
    print("测试: 模拟盘完整订单状态机链路")
    print("=" * 70)

    # 1. 初始化配置 (模拟盘 + 完整链路 + 0.5s 延迟)
    config = {
        "futu_trade_env": 1,  # 模拟盘
        "simulate_realistic_flow": True,
        "simulate_fill_delay_seconds": 0.5,
        "simulate_partial_fill": False,
        "idempotency_window": 3600,
        "brokers": [{"name": "futu", "priority": 1, "available": True}],
    }

    event_store = EventStore(db_path=":memory:")
    engine = ExecutionEngine(config, event_store)
    engine.initialize()

    # 2. 创建测试订单 (使用唯一 ID 避免幂等冲突)
    unique_id = f"CLIENT-001-{datetime.now().strftime('%H%M%S%f')}"
    order = Order(
        order_id="TEST-001",
        client_order_id=unique_id,
        symbol="US.AAPL",
        direction=Direction.LONG,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=150.0,
    )

    print(f"\n[1] 订单创建: {order.symbol} {order.direction.value} @ ${order.price}")
    print(f"    状态: {order.state.value if hasattr(order.state, 'value') else order.state}")

    # 3. 提交订单 (模拟盘完整链路)
    print(f"\n[2] 提交订单 (模拟盘完整链路)...")
    print(f"    simulate_realistic_flow=True, simulate_fill_delay_seconds=0.5s")

    try:
        result = engine.submit_order(order)

        # 4. 检查初始状态 (提交后应立即为 SENT)
        state_machine = engine.state_machines.get(order.order_id)
        initial_state = state_machine.current_state if state_machine else None
        print(f"\n[3] 提交后初始状态: {initial_state.value if initial_state else 'N/A'}")
        print(f"    历史: {[s.value for s in state_machine.history]}")

        # 验证: 必须包含 SENT 状态
        history = [s.value for s in state_machine.history]
        assert "SENT" in history, f"FAIL: SENT 不在历史状态 {history} 中"

        # 5. 等待 ACKED 阶段 (0.5s 延迟后)
        print(f"\n[4] 等待 simulate_fill_delay_seconds=0.5s...")
        time.sleep(0.6)  # 等待 ACK→FILLED 完成

        # 6. 检查最终状态
        final_state = state_machine.current_state
        print(f"\n[5] 最终状态: {final_state.value if final_state else 'N/A'}")
        print(f"    完整历史: {history}")

        # 验证: 最终状态必须是 FILLED
        assert final_state == OrderState.FILLED, f"FAIL: 最终状态应为 FILLED, 实际是 {final_state}"

        # 验证: 完整链路
        expected_flow = ["CREATED", "VALIDATED", "SENT", "ACKED", "FILLED"]
        if history == expected_flow:
            print(f"\n    ✅ 状态机完整链路验证通过!")
            print(f"       CREATED → VALIDATED → SENT → ACKED → FILLED")
        else:
            # 兼容模式 (无 ACKED 中间状态)
            expected_compat = ["CREATED", "VALIDATED", "FILLED"]
            if history == expected_compat:
                print(f"\n    ⚠️  状态机走的是兼容模式 (无中间状态)")
                print(f"       期望: {expected_flow}")
                print(f"       实际: {history}")
                return False
            else:
                print(f"\n    ❌ 状态机链路异常!")
                print(f"       期望: {expected_flow}")
                print(f"       实际: {history}")
                return False

        # 验证: 订单属性
        assert result.fill_price == 150.0, f"FAIL: fill_price 应为 150.0, 实际是 {result.fill_price}"
        assert result.filled_quantity == 100, f"FAIL: filled_quantity 应为 100, 实际是 {result.filled_quantity}"
        assert result.sent_at is not None, "FAIL: sent_at 不应为 None"
        assert result.filled_at is not None, "FAIL: filled_at 不应为 None"

        print(f"\n    ✅ 订单属性验证通过!")
        print(f"       fill_price: ${result.fill_price}")
        print(f"       filled_quantity: {result.filled_quantity}")
        print(f"       sent_at: {result.sent_at}")
        print(f"       filled_at: {result.filled_at}")

        return True

    except Exception as e:
        print(f"\n    ❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False

    finally:
        engine.shutdown()
        event_store.close()


def test_partial_fill_path():
    """测试部分成交路径"""
    print("\n" + "=" * 70)
    print("测试: 模拟盘部分成交路径 (PARTIALLY_FILLED)")
    print("=" * 70)

    config = {
        "futu_trade_env": 1,
        "simulate_realistic_flow": True,
        "simulate_fill_delay_seconds": 0.0,
        "simulate_partial_fill": True,  # 触发部分成交
        "idempotency_window": 3600,
        "brokers": [{"name": "futu", "priority": 1, "available": True}],
    }

    event_store = EventStore(db_path=":memory:")
    engine = ExecutionEngine(config, event_store)
    engine.initialize()

    order = Order(
        order_id="TEST-002",
        client_order_id=f"CLIENT-002-{datetime.now().strftime('%H%M%S%f')}",
        symbol="US.TSLA",
        direction=Direction.LONG,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=200.0,
    )

    print(f"\n[1] 创建订单: {order.symbol} qty={order.quantity} (触发部分成交)")

    try:
        result = engine.submit_order(order)
        state_machine = engine.state_machines.get(order.order_id)
        history = [s.value for s in state_machine.history]

        print(f"\n[2] 状态历史: {history}")

        # 验证: 必须包含 PARTIALLY_FILLED
        if "PARTIALLY_FILLED" in history:
            print(f"\n    ✅ PARTIALLY_FILLED 路径验证通过!")
            print(f"       filled_quantity: {result.filled_quantity}")
            return True
        else:
            print(f"\n    ⚠️  PARTIALLY_FILLED 未出现在历史状态中")
            print(f"       实际历史: {history}")
            return False

    except Exception as e:
        print(f"\n    ❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False

    finally:
        engine.shutdown()
        event_store.close()


def test_compatibility_mode():
    """测试兼容模式 (旧逻辑: 直接 FILLED)"""
    print("\n" + "=" * 70)
    print("测试: 兼容模式 (simulate_realistic_flow=False)")
    print("=" * 70)

    config = {
        "futu_trade_env": 1,
        "simulate_realistic_flow": False,  # 兼容模式
        "idempotency_window": 3600,
        "brokers": [{"name": "futu", "priority": 1, "available": True}],
    }

    event_store = EventStore(db_path=":memory:")
    engine = ExecutionEngine(config, event_store)
    engine.initialize()

    order = Order(
        order_id="TEST-003",
        client_order_id=f"CLIENT-003-{datetime.now().strftime('%H%M%S%f')}",
        symbol="US.NVDA",
        direction=Direction.LONG,
        order_type=OrderType.LIMIT,
        quantity=50,
        price=300.0,
    )

    print(f"\n[1] 创建订单: {order.symbol} (兼容模式)")

    try:
        result = engine.submit_order(order)
        state_machine = engine.state_machines.get(order.order_id)
        history = [s.value for s in state_machine.history]

        print(f"\n[2] 状态历史: {history}")

        # 兼容模式下应为: CREATED → VALIDATED → FILLED
        expected = ["CREATED", "VALIDATED", "FILLED"]
        if history == expected:
            print(f"\n    ✅ 兼容模式验证通过!")
            print(f"       CREATED → VALIDATED → FILLED")
            return True
        else:
            print(f"\n    ⚠️  兼容模式状态异常")
            print(f"       期望: {expected}")
            print(f"       实际: {history}")
            return False

    except Exception as e:
        print(f"\n    ❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False

    finally:
        engine.shutdown()
        event_store.close()


if __name__ == "__main__":
    print("\n" + "=" * 70)
    print("FTSeek-US 模拟盘状态机完整链路测试套件")
    print("=" * 70)

    results = []

    # 测试 1: 完整链路 (主测试)
    results.append(("完整链路 (simulate_realistic_flow=True)", test_full_order_state_flow()))

    # 测试 2: 部分成交路径
    results.append(("部分成交路径 (simulate_partial_fill=True)", test_partial_fill_path()))

    # 测试 3: 兼容模式
    results.append(("兼容模式 (simulate_realistic_flow=False)", test_compatibility_mode()))

    # 汇总
    print("\n" + "=" * 70)
    print("测试汇总")
    print("=" * 70)
    for name, passed in results:
        status = "✅ 通过" if passed else "❌ 失败"
        print(f"  {status}: {name}")

    all_passed = all(r[1] for r in results)
    print(f"\n总计: {sum(1 for r in results if r[1])}/{len(results)} 通过")

    sys.exit(0 if all_passed else 1)
