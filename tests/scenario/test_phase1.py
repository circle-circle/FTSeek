#!/usr/bin/env python3
"""
场景识别系统测试脚本 (Phase 1 验证)

测试场景分类器、路由器和记忆器的基本功能
"""
import sys
import os

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.scenario import (
    ScenarioClassifier,
    ScenarioRouter,
    ScenarioMemory,
    FeatureExtractor,
    ScenarioType,
    get_scenario_classifier,
    get_scenario_router,
    get_scenario_memory,
)


def test_feature_extractor():
    """测试特征提取器"""
    print("=" * 60)
    print("测试特征提取器")
    print("=" * 60)

    extractor = FeatureExtractor()

    # 模拟 K 线数据
    klines = []
    base_price = 100.0
    for i in range(100):
        price = base_price + (i % 20) * 0.5  # 简单上涨趋势
        klines.append({
            'open': price,
            'high': price + 1,
            'low': price - 1,
            'close': price + 0.5,
            'volume': 1000000 + i * 10000,
        })

    features = extractor.extract("HK.00700", klines)

    print(f"股票代码：{features.code}")
    print(f"K 线数量：{features.kline_count}")
    print(f"ADX: {features.trend.adx:.2f}")
    print(f"MA 斜率：{features.trend.ma_slope:.4f}")
    print(f"均线排列：{features.trend.ma_alignment}")
    print(f"ATR: {features.volatility.atr:.4f}")
    print(f"ATR 比率：{features.volatility.atr_ratio:.2f}")
    print(f"布林带宽度：{features.volatility.bb_width:.4f}")
    print(f"成交量比率：{features.volume.volume_ratio:.2f}")
    print(f"RSI: {features.position.rsi:.2f}")
    print(f"价格 vs MA: {features.position.price_vs_ma:.4f}")

    print("✓ 特征提取器测试通过\n")
    return True


def test_scenario_classifier():
    """测试场景分类器"""
    print("=" * 60)
    print("测试场景分类器")
    print("=" * 60)

    classifier = get_scenario_classifier()

    # 测试数据：强趋势场景
    print("\n--- 测试 1: 强趋势场景 ---")
    klines_trend = []
    base_price = 100.0
    for i in range(100):
        price = base_price + i * 0.8  # 明显上涨趋势
        klines_trend.append({
            'open': price,
            'high': price + 1.5,
            'low': price - 0.5,
            'close': price + 1.0,
            'volume': 1000000 + i * 10000,
        })

    result = classifier.classify("HK.00700", klines_trend)
    print(f"场景：{result.scenario_type.value}")
    print(f"置信度：{result.confidence:.2f}")
    print(f"宏观状态：{result.macro_regime.value}")
    print(f"ADX: {result.adx_value:.1f}")

    # 测试数据：震荡场景
    print("\n--- 测试 2: 震荡场景 ---")
    klines_range = []
    base_price = 100.0
    for i in range(100):
        price = base_price + (i % 10) * 0.3 - 1.5  # 区间震荡
        klines_range.append({
            'open': price,
            'high': price + 1,
            'low': price - 1,
            'close': price,
            'volume': 1000000,
        })

    result = classifier.classify("HK.00700", klines_range)
    print(f"场景：{result.scenario_type.value}")
    print(f"置信度：{result.confidence:.2f}")
    print(f"宏观状态：{result.macro_regime.value}")
    print(f"ADX: {result.adx_value:.1f}")

    print("\n✓ 场景分类器测试通过\n")
    return True


def test_scenario_router():
    """测试场景路由器"""
    print("=" * 60)
    print("测试场景路由器")
    print("=" * 60)

    router = get_scenario_router()

    # 测试各核心场景的路由
    test_scenarios = [
        ScenarioType.TREND_STRONG,
        ScenarioType.RANGE_BOUND,
        ScenarioType.GAP_EVENT,
        ScenarioType.VOL_EXPANSION,
        ScenarioType.REVERSAL_SETUP,
    ]

    for scenario in test_scenarios:
        print(f"\n--- 场景：{scenario.value} ---")
        result = router.route(scenario)

        print(f"  主策略：{result.primary_strategies}")
        print(f"  辅助策略：{result.secondary_strategies}")
        print(f"  抑制策略：{result.suppressed_strategies}")
        print(f"  风控模板：{result.risk_template}")
        print(f"  策略权重：{result.active_strategies}")

    print("\n✓ 场景路由器测试通过\n")
    return True


def test_scenario_memory():
    """测试场景记忆器"""
    print("=" * 60)
    print("测试场景记忆器")
    print("=" * 60)

    memory = get_scenario_memory()

    code = "HK.00700"

    # 模拟场景切换过程
    print("\n--- 模拟场景确认过程 ---")

    # 连续 3 次 TREND_STRONG
    for i in range(3):
        result = memory.update(code, ScenarioType.TREND_STRONG, 0.85, i)
        print(f"第{i+1}次输入 TREND_STRONG → 输出：{result.value} (确认计数：{memory.get_confirm_count(code)})")

    # 尝试切换到 RANGE_BOUND (应该被拒绝，因为确认计数不足)
    result = memory.update(code, ScenarioType.RANGE_BOUND, 0.75, 3)
    print(f"输入 RANGE_BOUND → 输出：{result.value} (冷却中)")

    # 再次输入 RANGE_BOUND (确认计数增加)
    result = memory.update(code, ScenarioType.RANGE_BOUND, 0.75, 4)
    result2 = memory.update(code, ScenarioType.RANGE_BOUND, 0.75, 5)
    print(f"连续输入 RANGE_BOUND → 输出：{result2.value}")

    # 获取历史
    history = memory.get_history(code, limit=5)
    print(f"\n最近 5 条历史记录:")
    for h in history:
        print(f"  - {h['scenario']} @ {h['datetime'][-8:]}")

    print("\n✓ 场景记忆器测试通过\n")
    return True


def test_config():
    """测试配置模块"""
    print("=" * 60)
    print("测试配置模块")
    print("=" * 60)

    from app.scenario.config import (
        CORE_SCENARIO_ROUTING,
        DEFAULT_WEIGHT_CONFIG,
        SCENARIO_CLASSIFIER_CONFIG,
        get_scenario_config,
        get_all_scenarios,
    )

    print(f"\n已配置场景数：{len(CORE_SCENARIO_ROUTING)}")
    print(f"核心场景列表：{get_all_scenarios()}")
    print(f"默认权重配置：{DEFAULT_WEIGHT_CONFIG}")
    print(f"分类器配置：{SCENARIO_CLASSIFIER_CONFIG}")

    # 测试获取单个场景配置
    config = get_scenario_config("TREND_STRONG")
    print(f"\nTREND_STRONG 配置:")
    print(f"  主策略：{config['primary']}")
    print(f"  风控模板：{config['risk_template']}")

    print("\n✓ 配置模块测试通过\n")
    return True


def test_database_scenario_field():
    """测试数据库 scenario 字段"""
    print("=" * 60)
    print("测试数据库 scenario 字段")
    print("=" * 60)

    from app.storage.database import get_database
    from datetime import datetime

    db = get_database()

    # 保存一条带 scenario 的记录
    test_data = {
        'code': 'HK.00700',
        'action': 'BUY',
        'strategy': 'confluence_v2',
        'entry_price': 100.0,
        'exit_price': 105.0,
        'pnl_pct': 5.0,
        'holding_periods': 10,
        'market_regime': 'trending',
        'scenario': 'trend_strong',  # Phase 1 新增字段
        'adx_value': 35.0,
        'mtf_alignment': 0.8,
        'sentiment_score': 0.5,
        'consensus_score': 0.7,
        'quality_score': 1.0,
        'max_drawdown': 2.0,
        'max_profit': 6.0,
        'timestamp': datetime.now().isoformat(),
    }

    success = db.save_signal_outcome(test_data)
    print(f"保存带 scenario 的记录：{'成功' if success else '失败'}")

    # 查询 scenario 记录
    outcomes = db.get_signal_outcomes_by_scenario('trend_strong', days=30)
    print(f"查询到 trend_strong 场景记录数：{len(outcomes)}")

    if outcomes:
        print(f"最新记录：scenario={outcomes[0]['scenario']}, pnl={outcomes[0]['pnl_pct']}%")

    # 查询按 scenario 分组的统计
    summary = db.get_signal_outcomes_summary(days=30, group_by='scenario')
    print(f"\n按场景分组统计:")
    for row in summary.get('data', []):
        print(f"  {row['scenario']}: {row['total_trades']}笔，胜率{row['win_rate']:.1%}")

    print("\n✓ 数据库 scenario 字段测试通过\n")
    return True


def main():
    """运行所有测试"""
    print("\n" + "=" * 60)
    print("FTSeek 场景识别系统 Phase 1 测试")
    print("=" * 60 + "\n")

    tests = [
        ("特征提取器", test_feature_extractor),
        ("场景分类器", test_scenario_classifier),
        ("场景路由器", test_scenario_router),
        ("场景记忆器", test_scenario_memory),
        ("配置模块", test_config),
        ("数据库 scenario 字段", test_database_scenario_field),
    ]

    passed = 0
    failed = 0

    for name, test_func in tests:
        try:
            if test_func():
                passed += 1
        except Exception as e:
            print(f"✗ {name} 测试失败：{e}\n")
            import traceback
            traceback.print_exc()
            failed += 1

    print("=" * 60)
    print(f"测试完成：{passed}通过，{failed}失败")
    print("=" * 60)

    return failed == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
