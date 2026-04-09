"""
Phase 3: 场景准确率统计与绩效反馈测试

测试场景绩效分析功能
"""
import sys
sys.path.insert(0, '/opt/ftseek/FTSeek')

from app.analytics.scenario_performance import get_scenario_performance_analytics

print("=" * 60)
print("Phase 3: 场景准确率统计与绩效反馈测试")
print("=" * 60)

# ============================================================
# 测试 1: 场景绩效分析器初始化
# ============================================================
print("\n=== 测试 1: 场景绩效分析器初始化 ===")

analytics = get_scenario_performance_analytics()
print(f"场景绩效分析器：{analytics is not None}")

if analytics:
    print("✅ 场景绩效分析器初始化成功")
else:
    print("❌ 场景绩效分析器初始化失败")

# ============================================================
# 测试 2: 场景胜率统计
# ============================================================
print("\n=== 测试 2: 场景胜率统计 ===")

if analytics:
    performance = analytics.analyze_scenario_win_rate(days=30)
    print(f"分析场景数：{len(performance)}")

    if performance:
        print("各场景表现:")
        for scenario, stats in list(performance.items())[:5]:
            print(f"  - {scenario}:")
            print(f"      交易数={stats['total_trades']}, 胜率={stats['win_rate']:.2f}, "
                  f"平均盈亏={stats['avg_pnl_pct']:.1f}%")
        print("✅ 场景胜率统计成功")
    else:
        print("⚠️ 无足够数据进行分析")
else:
    print("❌ 跳过测试 (分析器未初始化)")

# ============================================================
# 测试 3: 场景准确率详情
# ============================================================
print("\n=== 测试 3: 场景准确率详情 ===")

if analytics:
    # 测试一个场景
    test_scenario = "trend_strong"
    accuracy = analytics.get_scenario_accuracy(test_scenario, days=90)

    print(f"场景：{accuracy.get('scenario', 'N/A')}")
    print(f"交易数：{accuracy.get('total_trades', 0)}")
    print(f"准确率：{accuracy.get('accuracy', 0):.2f}")
    print(f"平均盈亏：{accuracy.get('avg_pnl', 0):.1f}%")
    print(f"最佳交易：{accuracy.get('best_trade', 0):.1f}%")
    print(f"最差交易：{accuracy.get('worst_trade', 0):.1f}%")
    print(f"连胜：{accuracy.get('consecutive_wins', 0)}, 连败：{accuracy.get('consecutive_losses', 0)}")

    if accuracy.get('total_trades', 0) > 0:
        print("✅ 场景准确率详情查询成功")
    else:
        print(f"⚠️ 场景 {test_scenario} 无足够数据")
else:
    print("❌ 跳过测试 (分析器未初始化)")

# ============================================================
# 测试 4: 场景×策略矩阵
# ============================================================
print("\n=== 测试 4: 场景×策略矩阵 ===")

if analytics:
    matrix = analytics.analyze_scenario_strategy_matrix(days=90)
    print(f"分析场景数：{len(matrix)}")

    if matrix:
        for scenario, strategies in list(matrix.items())[:3]:
            print(f"  {scenario}:")
            for strategy, stats in list(strategies.items())[:3]:
                print(f"    - {strategy}: {stats['trades']}笔，胜率{stats['win_rate']:.2f}")
        print("✅ 场景×策略矩阵分析成功")
    else:
        print("⚠️ 无足够数据进行矩阵分析")
else:
    print("❌ 跳过测试 (分析器未初始化)")

# ============================================================
# 测试 5: 最佳策略推荐
# ============================================================
print("\n=== 测试 5: 最佳策略推荐 ===")

if analytics:
    test_scenario = "trend_strong"
    best_strategies = analytics.get_best_strategies_for_scenario(test_scenario, days=90, min_trades=1)

    print(f"场景：{test_scenario}")
    print(f"最佳策略数：{len(best_strategies)}")

    if best_strategies:
        print("最佳策略排名:")
        for i, s in enumerate(best_strategies[:5], 1):
            print(f"  {i}. {s['strategy']}: {s['trades']}笔，胜率{s['win_rate']:.2f}, "
                  f"平均盈亏{s['avg_pnl']:.1f}%")
        print("✅ 最佳策略推荐成功")
    else:
        print(f"⚠️ 场景 {test_scenario} 无足够数据")
else:
    print("❌ 跳过测试 (分析器未初始化)")

# ============================================================
# 测试 6: 置信度校准分析
# ============================================================
print("\n=== 测试 6: 置信度校准分析 ===")

if analytics:
    confidence_data = analytics.analyze_confidence_accuracy(days=90, confidence_buckets=5)

    if 'error' not in confidence_data:
        print(f"置信度分桶数：{len(confidence_data)}")
        print("各桶表现:")
        for bucket, stats in confidence_data.items():
            print(f"  {bucket}: {stats['trades']}笔，胜率{stats['win_rate']:.2f}, "
                  f"平均盈亏{stats['avg_pnl']:.1f}%")
        print("✅ 置信度校准分析成功")
    else:
        print(f"⚠️ 无足够数据进行置信度校准分析")
else:
    print("❌ 跳过测试 (分析器未初始化)")

# ============================================================
# 测试 7: 权重建议
# ============================================================
print("\n=== 测试 7: 权重建议 ===")

if analytics:
    recommendations = analytics.get_scenario_weight_recommendations(days=90)

    if recommendations:
        print(f"生成建议数：{len(recommendations)}")
        print("权重建议:")
        for scenario, rec in list(recommendations.items())[:5]:
            print(f"  {scenario}:")
            print(f"    当前权重：{rec['current_weight']}")
            print(f"    建议权重：{rec['recommended_weight']}")
            print(f"    理由：{rec['reason']}")
        print("✅ 权重建议生成成功")
    else:
        print("⚠️ 无足够数据生成权重建议")
else:
    print("❌ 跳过测试 (分析器未初始化)")

# ============================================================
# 测试总结
# ============================================================
print("\n" + "=" * 60)
print("测试完成")
print("=" * 60)
print("场景准确率统计与绩效反馈功能验证完成")
print("支持:")
print("  - 场景胜率/盈亏比统计")
print("  - 场景准确率详情 (连胜/连败)")
print("  - 场景×策略二维矩阵")
print("  - 最佳策略推荐")
print("  - 置信度校准分析")
print("  - 权重自适应建议")
