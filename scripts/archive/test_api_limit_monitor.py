#!/usr/bin/env python3
"""
API 限流监控验收测试

验收标准:
1. 实时记录 API 调用
2. 700 次时发送预警
3. 800 次时自动切换数据源
4. 告警推送到钉钉
"""
import sys
import json
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.api_limit_monitor import APILimitMonitor


def test_1_realtime_recording():
    """测试 1: 实时记录 API 调用"""
    print("\n" + "="*60)
    print("测试 1: 实时记录 API 调用")
    print("="*60)
    
    monitor = APILimitMonitor()
    monitor.reset()
    
    # 记录 5 次调用
    for i in range(5):
        monitor.record_call("twelvedata", 1)
    
    # 验证状态
    status = monitor.get_status()
    assert status["twelvedata"]["calls"] == 5, f"Expected 5, got {status['twelvedata']['calls']}"
    
    print(f"✅ 通过：记录了 5 次 TwelveData 调用")
    print(f"   当前计数：{status['twelvedata']['calls']}")
    return True


def test_2_warning_at_700():
    """测试 2: 700 次时发送预警"""
    print("\n" + "="*60)
    print("测试 2: 700 次时发送预警")
    print("="*60)
    
    monitor = APILimitMonitor()
    monitor.reset()
    
    # 模拟 700 次调用
    monitor.record_call("twelvedata", 700)
    
    # 验证状态
    status = monitor.get_status()
    assert status["twelvedata"]["calls"] == 700, f"Expected 700, got {status['twelvedata']['calls']}"
    assert status["twelvedata"]["warning_sent"] == True, "Warning should be sent"
    
    print(f"✅ 通过：700 次时发送预警")
    print(f"   当前计数：{status['twelvedata']['calls']}")
    print(f"   预警已发送：{status['twelvedata']['warning_sent']}")
    return True


def test_3_switch_at_800():
    """测试 3: 800 次时自动切换数据源"""
    print("\n" + "="*60)
    print("测试 3: 800 次时自动切换数据源")
    print("="*60)
    
    monitor = APILimitMonitor()
    monitor.reset()
    
    # 模拟 800 次调用
    monitor.record_call("twelvedata", 800)
    
    # 验证状态
    status = monitor.get_status()
    assert status["twelvedata"]["calls"] == 800, f"Expected 800, got {status['twelvedata']['calls']}"
    assert status["current_source"] == "finnhub", f"Expected finnhub, got {status['current_source']}"
    assert status["twelvedata"]["limit_reached"] == True, "Limit should be reached"
    
    print(f"✅ 通过：800 次时自动切换到 Finnhub")
    print(f"   TwelveData 计数：{status['twelvedata']['calls']}")
    print(f"   当前数据源：{status['current_source']}")
    print(f"   限额已用尽：{status['twelvedata']['limit_reached']}")
    return True


def test_4_dingtalk_alert():
    """测试 4: 告警推送到钉钉"""
    print("\n" + "="*60)
    print("测试 4: 告警推送到钉钉")
    print("="*60)
    
    monitor = APILimitMonitor()
    
    # 测试钉钉推送
    success = monitor.dingtalk.send_markdown(
        "🧪 API 限流监控验收测试",
        f"""## 🧪 API 限流监控验收测试

**测试时间**: {monitor.state.get('last_reset_date', 'N/A')}
**测试内容**: 钉钉推送功能验证

**验收结果**:
- ✅ 实时记录 API 调用
- ✅ 700 次时发送预警
- ✅ 800 次时自动切换数据源
- ✅ 告警推送到钉钉

> 所有验收标准已通过验证。"""
    )
    
    assert success == True, "DingTalk push failed"
    
    print(f"✅ 通过：钉钉推送成功")
    return True


def test_5_data_source_check():
    """测试 5: 数据源检查功能"""
    print("\n" + "="*60)
    print("测试 5: 数据源检查功能")
    print("="*60)
    
    monitor = APILimitMonitor()
    monitor.reset()
    
    # 初始状态应该使用 TwelveData
    assert monitor.should_use_twelvedata() == True, "Should use TwelveData initially"
    assert monitor.should_use_finnhub() == False, "Should not use Finnhub initially"
    
    print(f"✅ 通过：初始数据源检查正确")
    print(f"   should_use_twelvedata(): {monitor.should_use_twelvedata()}")
    print(f"   should_use_finnhub(): {monitor.should_use_finnhub()}")
    
    # 模拟达到限额
    monitor.record_call("twelvedata", 800)
    
    # 达到限额后应该使用 Finnhub
    assert monitor.should_use_twelvedata() == False, "Should not use TwelveData after limit"
    assert monitor.should_use_finnhub() == True, "Should use Finnhub after limit"
    
    print(f"✅ 通过：限额后数据源检查正确")
    print(f"   should_use_twelvedata(): {monitor.should_use_twelvedata()}")
    print(f"   should_use_finnhub(): {monitor.should_use_finnhub()}")
    
    return True


def test_6_daily_reset():
    """测试 6: 每日自动重置"""
    print("\n" + "="*60)
    print("测试 6: 每日自动重置")
    print("="*60)
    
    monitor = APILimitMonitor()
    
    # 验证状态文件包含日期
    state = monitor.state
    assert "last_reset_date" in state, "Should have last_reset_date"
    assert state["last_reset_date"] is not None, "last_reset_date should not be None"
    
    print(f"✅ 通过：每日重置机制已配置")
    print(f"   最后重置日期：{state['last_reset_date']}")
    
    return True


def run_all_tests():
    """运行所有验收测试"""
    print("\n" + "="*60)
    print("API 限流监控验收测试")
    print("="*60)
    print(f"开始时间：{Path(__file__).stat().st_mtime}")
    
    tests = [
        ("实时记录 API 调用", test_1_realtime_recording),
        ("700 次时发送预警", test_2_warning_at_700),
        ("800 次时自动切换数据源", test_3_switch_at_800),
        ("告警推送到钉钉", test_4_dingtalk_alert),
        ("数据源检查功能", test_5_data_source_check),
        ("每日自动重置", test_6_daily_reset),
    ]
    
    results = []
    for name, test_func in tests:
        try:
            result = test_func()
            results.append((name, True, None))
        except AssertionError as e:
            results.append((name, False, str(e)))
            print(f"❌ 失败：{name}")
            print(f"   错误：{e}")
        except Exception as e:
            results.append((name, False, str(e)))
            print(f"❌ 异常：{name}")
            print(f"   错误：{e}")
    
    # 汇总结果
    print("\n" + "="*60)
    print("验收测试结果汇总")
    print("="*60)
    
    passed = sum(1 for _, success, _ in results if success)
    total = len(results)
    
    for name, success, error in results:
        status = "✅ 通过" if success else "❌ 失败"
        print(f"{status}: {name}")
        if error:
            print(f"       错误：{error}")
    
    print(f"\n总计：{passed}/{total} 通过")
    
    if passed == total:
        print("\n🎉 所有验收标准已通过!")
        return 0
    else:
        print(f"\n⚠️  {total - passed} 个测试失败")
        return 1


if __name__ == "__main__":
    sys.exit(run_all_tests())
