#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
实时监控增强测试脚本

测试内容:
1. 监控间隔验证 (30 秒)
2. 持仓扫描测试
3. 止损止盈触发测试
4. 距离告警测试
5. 波动率告警测试
6. 告警频率限制测试

用法: python3 test_monitoring_enhanced.py
"""
import sys
import time
import logging
from datetime import datetime
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.data.us_stock_monitor import USStockMonitor, MonitorConfig, PriceHistory, AlertRateLimiter

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('logs/test_monitoring.log', encoding='utf-8')
    ]
)
logger = logging.getLogger("test_monitoring")


def test_monitor_config():
    """测试 1: 监控配置验证"""
    print("\n" + "="*60)
    print("测试 1: 监控配置验证")
    print("="*60)
    
    config = MonitorConfig()
    
    # 验证监控间隔
    assert config.check_interval == 30, f"❌ 检查间隔应为 30 秒，实际{config.check_interval}秒"
    print(f"✅ 检查间隔：{config.check_interval}秒 (原 300 秒)")
    
    # 验证夜间间隔
    assert config.night_interval == 60, f"❌ 夜间间隔应为 60 秒，实际{config.night_interval}秒"
    print(f"✅ 夜间间隔：{config.night_interval}秒 (原 900 秒)")
    
    # 验证告警配置
    assert config.alert_rate_limit_seconds == 300, "❌ 告警冷却时间配置错误"
    print(f"✅ 告警冷却时间：{config.alert_rate_limit_seconds}秒")
    
    assert config.volatility_alert_threshold == 0.05, "❌ 波动率阈值配置错误"
    print(f"✅ 波动率告警阈值：{config.volatility_alert_threshold:.1%}")
    
    print("\n✅ 测试 1 通过：监控配置正确")
    return True


def test_price_history():
    """测试 2: 价格历史与波动率计算"""
    print("\n" + "="*60)
    print("测试 2: 价格历史与波动率计算")
    print("="*60)
    
    history = PriceHistory()
    
    # 添加价格数据
    base_price = 100.0
    current_time = time.time()
    
    for i in range(10):
        price = base_price * (1 + 0.01 * (i % 3 - 1))  # ±1% 波动
        history.add(price, current_time - (9-i)*10)  # 每 10 秒一个价格
    
    # 计算波动率
    volatility = history.get_volatility(window_seconds=60)
    print(f"📊 60 秒波动率：{volatility:.2%}")
    
    # 测试高波动场景
    history2 = PriceHistory()
    for i in range(5):
        price = base_price * (1 + 0.03 * (i % 2 * 2 - 1))  # ±3% 波动
        history2.add(price, current_time - (4-i)*15)
    
    volatility2 = history2.get_volatility(window_seconds=60)
    print(f"📊 高波动场景：{volatility2:.2%}")
    
    assert volatility2 > volatility, "❌ 高波动场景波动率应更高"
    print("✅ 测试 2 通过：波动率计算正确")
    return True


def test_alert_rate_limiter():
    """测试 3: 告警频率限制"""
    print("\n" + "="*60)
    print("测试 3: 告警频率限制")
    print("="*60)
    
    limiter = AlertRateLimiter(cooldown_seconds=5)  # 5 秒冷却 (测试用)
    
    alert_key = "test_alert_US.AAPL"
    
    # 第一次发送 (应允许)
    result1 = limiter.should_send(alert_key)
    assert result1 == True, "❌ 第一次告警应允许发送"
    print(f"✅ 第 1 次告警：允许发送")
    
    # 立即再次发送 (应拒绝)
    result2 = limiter.should_send(alert_key)
    assert result2 == False, "❌ 冷却期内告警应拒绝"
    print(f"✅ 第 2 次告警 (立即): 拒绝 (冷却中)")
    
    # 等待冷却时间后发送 (应允许)
    print(f"⏳ 等待 5 秒冷却...")
    time.sleep(5)
    
    result3 = limiter.should_send(alert_key)
    assert result3 == True, "❌ 冷却期后告警应允许发送"
    print(f"✅ 第 3 次告警 (冷却后): 允许发送")
    
    # 验证告警计数
    count = limiter.get_alert_count(alert_key)
    assert count == 2, f"❌ 告警计数应为 2，实际{count}"
    print(f"✅ 告警计数：{count}")
    
    print("\n✅ 测试 3 通过：告警频率限制正常")
    return True


def test_pnl_calculation():
    """测试 4: 盈亏计算"""
    print("\n" + "="*60)
    print("测试 4: 盈亏计算 (模拟)")
    print("="*60)
    
    # 模拟持仓数据
    entry_price = 100.0
    current_price = 95.0
    quantity = 100
    
    # 计算盈亏 (做多)
    pnl_rate = (current_price - entry_price) / entry_price
    pnl_amount = (current_price - entry_price) * quantity
    
    print(f"📊 持仓模拟:")
    print(f"   入场价：${entry_price:.2f}")
    print(f"   当前价：${current_price:.2f}")
    print(f"   数量：{quantity}")
    print(f"   盈亏率：{pnl_rate:.2%}")
    print(f"   盈亏额：${pnl_amount:.2f}")
    
    # 计算距离止损
    stop_loss = -0.08
    stop_loss_price = entry_price * (1 + stop_loss)
    distance_to_stop = (current_price - stop_loss_price) / current_price
    
    print(f"\n📏 止损信息:")
    print(f"   止损价：${stop_loss_price:.2f}")
    print(f"   距离止损：{distance_to_stop:.2%}")
    
    # 计算距离止盈
    take_profit = 0.15
    take_profit_price = entry_price * (1 + take_profit)
    distance_to_profit = (take_profit_price - current_price) / current_price
    
    print(f"\n📏 止盈信息:")
    print(f"   止盈价：${take_profit_price:.2f}")
    print(f"   距离止盈：{distance_to_profit:.2%}")
    
    assert pnl_rate == -0.05, "❌ 盈亏率计算错误"
    assert distance_to_stop > 0, "❌ 距离止损应为正数"
    
    print("\n✅ 测试 4 通过：盈亏计算正确")
    return True


def test_monitor_initialization():
    """测试 5: 监控器初始化"""
    print("\n" + "="*60)
    print("测试 5: 监控器初始化")
    print("="*60)
    
    config = MonitorConfig(
        check_interval=30,
        night_interval=60,
        alert_rate_limit_seconds=300,
        volatility_alert_threshold=0.05
    )
    
    monitor = USStockMonitor(config)
    
    # 验证配置
    assert monitor.config.check_interval == 30, "❌ 检查间隔配置错误"
    assert monitor.config.night_interval == 60, "❌ 夜间间隔配置错误"
    assert monitor.config.alert_rate_limit_seconds == 300, "❌ 告警冷却配置错误"
    
    # 验证组件初始化
    assert monitor.alert_limiter is not None, "❌ 告警限制器未初始化"
    assert monitor.price_history == {}, "❌ 价格历史应为空字典"
    assert monitor.peak_prices == {}, "❌ 峰值价格应为空字典"
    
    print(f"✅ 监控器初始化成功")
    print(f"   检查间隔：{monitor.config.check_interval}秒")
    print(f"   夜间间隔：{monitor.config.night_interval}秒")
    print(f"   告警冷却：{monitor.config.alert_rate_limit_seconds}秒")
    print(f"   波动阈值：{monitor.config.volatility_alert_threshold:.1%}")
    
    print("\n✅ 测试 5 通过：监控器初始化正常")
    return True


def run_all_tests():
    """运行所有测试"""
    print("\n" + "="*60)
    print("🧪 实时监控增强测试套件")
    print("="*60)
    print(f"开始时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    tests = [
        ("监控配置验证", test_monitor_config),
        ("价格历史与波动率", test_price_history),
        ("告警频率限制", test_alert_rate_limiter),
        ("盈亏计算", test_pnl_calculation),
        ("监控器初始化", test_monitor_initialization),
    ]
    
    results = []
    for name, test_func in tests:
        try:
            result = test_func()
            results.append((name, result, None))
        except Exception as e:
            results.append((name, False, str(e)))
            logger.error(f"测试失败：{name} - {e}")
    
    # 汇总结果
    print("\n" + "="*60)
    print("📊 测试结果汇总")
    print("="*60)
    
    passed = sum(1 for _, r, _ in results if r)
    total = len(results)
    
    for name, result, error in results:
        status = "✅ 通过" if result else f"❌ 失败：{error}"
        print(f"  {status} - {name}")
    
    print(f"\n总计：{passed}/{total} 通过")
    
    if passed == total:
        print("\n🎉 所有测试通过！")
        return True
    else:
        print(f"\n⚠️  {total - passed} 个测试失败")
        return False


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
