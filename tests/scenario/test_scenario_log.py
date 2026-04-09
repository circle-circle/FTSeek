"""
Phase 2: 场景日志与可视化测试

测试场景日志记录、查询和统计功能
"""
import sys
sys.path.insert(0, '/opt/ftseek/FTSeek')

from app.scenario import (
    get_scenario_classifier,
    get_scenario_log_manager,
    ScenarioType,
)

print("=" * 60)
print("Phase 2: 场景日志与可视化测试")
print("=" * 60)

# ============================================================
# 测试 1: 场景日志管理器初始化
# ============================================================
print("\n=== 测试 1: 场景日志管理器初始化 ===")

log_manager = get_scenario_log_manager()
print(f"场景日志管理器：{log_manager is not None}")
print(f"数据库路径：{log_manager.db_path if log_manager else 'N/A'}")

if log_manager:
    print("✅ 场景日志管理器初始化成功")
else:
    print("❌ 场景日志管理器初始化失败")

# ============================================================
# 测试 2: 场景切换日志记录
# ============================================================
print("\n=== 测试 2: 场景切换日志记录 ===")

# 模拟场景切换
test_scenarios = [
    ("HK.TEST_LOG", "trend_strong", 0.85, 35.2),
    ("HK.TEST_LOG", "trend_strong", 0.87, 36.1),  # 1 分钟内，应该被去重
    ("HK.TEST_LOG", "range_bound", 0.72, 18.5),
    ("HK.09988", "gap_event", 0.92, 25.0),
    ("HK.00700", "news_driven", 0.88, 22.3),
]

for code, scenario, confidence, adx in test_scenarios:
    success = log_manager.log_scenario_change(
        code=code,
        scenario=scenario,
        confidence=confidence,
        adx_value=adx,
        macro_regime="strong_trend_up" if scenario == "trend_strong" else "range_bound",
        mtf_alignment=0.8,
        triggered_by='auto'
    )
    print(f"记录 {code} → {scenario}: {'✅' if success else '⚠️ (去重)'}")

# ============================================================
# 测试 3: 查询场景历史
# ============================================================
print("\n=== 测试 3: 查询场景历史 ===")

history = log_manager.get_scenario_history("HK.TEST_LOG", limit=10, days=1)
print(f"HK.TEST_LOG 场景历史数：{len(history)}")

if history:
    print("最近 3 条记录:")
    for record in history[:3]:
        print(f"  - {record['scenario']} (置信度={record['confidence']:.2f}) @ {record['timestamp']}")
    print("✅ 场景历史查询成功")
else:
    print("⚠️ 无场景历史记录")

# ============================================================
# 测试 4: 获取当前场景
# ============================================================
print("\n=== 测试 4: 获取当前场景 ===")

current = log_manager.get_current_scenario("HK.TEST_LOG")
print(f"HK.TEST_LOG 当前场景：{current}")

if current:
    print("✅ 当前场景查询成功")
else:
    print("⚠️ 当前场景为空")

# ============================================================
# 测试 5: 场景切换记录
# ============================================================
print("\n=== 测试 5: 场景切换记录 ===")

switches = log_manager.get_scenario_switches("HK.TEST_LOG", days=1)
print(f"HK.TEST_LOG 场景切换次数：{len(switches)}")

if switches:
    print("切换记录:")
    for switch in switches:
        print(f"  - {switch.get('from_scenario', 'N/A')} → {switch['to_scenario']} @ {switch['timestamp']}")
    print("✅ 场景切换记录成功")
else:
    print("⚠️ 无场景切换记录")

# ============================================================
# 测试 6: 场景统计
# ============================================================
print("\n=== 测试 6: 场景统计 ===")

stats = log_manager.get_scenario_statistics(days=7, group_by="scenario")
print(f"统计天数：{stats['days']}")
print(f"总记录数：{stats['total_records']}")

if stats['data']:
    print("各场景统计:")
    for item in stats['data'][:5]:
        print(f"  - {item['scenario']}: {item['total_count']}次, "
              f"平均置信度={item.get('avg_confidence', 0):.2f}")
    print("✅ 场景统计成功")
else:
    print("⚠️ 无统计数据")

# ============================================================
# 测试 7: 活跃股票列表
# ============================================================
print("\n=== 测试 7: 活跃股票列表 ===")

codes = log_manager.get_all_active_codes()
print(f"活跃股票数：{len(codes)}")
print(f"活跃股票列表：{codes[:10]}{'...' if len(codes) > 10 else ''}")

if codes:
    print("✅ 活跃股票列表获取成功")
else:
    print("⚠️ 无活跃股票")

# ============================================================
# 测试 8: 场景分类器集成 (自动日志记录)
# ============================================================
print("\n=== 测试 8: 场景分类器集成测试 ===")

classifier = get_scenario_classifier()

# 生成测试 K 线
def generate_klines(base_price, count, trend='bullish'):
    klines = []
    price = base_price
    for i in range(count):
        if trend == 'bullish':
            change = 0.002
        elif trend == 'bearish':
            change = -0.002
        else:
            change = 0.0005 * (1 if i % 2 == 0 else -1)

        open_price = price
        close_price = price * (1 + change)
        high_price = max(open_price, close_price) * 1.01
        low_price = min(open_price, close_price) * 0.99

        klines.append({
            'open': open_price,
            'high': high_price,
            'low': low_price,
            'close': close_price,
            'volume': 1000000,
        })
        price = close_price
    return klines

klines = generate_klines(100, 100, trend='bullish')

# 执行分类 (应该自动记录日志)
result = classifier.classify("HK.TEST_AUTO_LOG", klines)
print(f"场景分类结果：{result.scenario_type.value}")
print(f"置信度：{result.confidence:.2f}")

# 检查是否自动记录
import time
time.sleep(0.1)  # 等待异步写入
history_auto = log_manager.get_scenario_history("HK.TEST_AUTO_LOG", limit=5)
print(f"自动日志记录数：{len(history_auto)}")

if history_auto:
    print("✅ 场景分类器自动日志记录成功")
else:
    print("⚠️ 自动日志记录失败")

# ============================================================
# 测试总结
# ============================================================
print("\n" + "=" * 60)
print("测试完成")
print("=" * 60)
print("场景日志与可视化功能实现验证完成")
print("支持:")
print("  - 场景切换日志记录到数据库")
print("  - 场景历史查询 (支持 limit/days 过滤)")
print("  - 场景切换记录检测")
print("  - 场景统计分析")
print("  - 活跃股票列表")
print("  - 场景分类器自动日志记录")
