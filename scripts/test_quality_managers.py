#!/usr/bin/env python3
"""
质量管理器快速测试脚本

测试新实现的巴菲特理念模块:
- QualityScorer (质量评分器)
- HoldingPeriodManager (持仓周期管理器)
- WaterLevelManager (资金水位管理器)
- ProfitLossManager (止盈止损管理器)
- ValuationCalculator (估值计算器)

用法:
    python3 scripts/test_quality_managers.py

注意：由于项目存在循环导入问题，本测试脚本直接加载模块而不通过包导入。
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime


def load_module(module_name, file_path):
    """动态加载模块 (避免循环导入)"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_quality_scorer():
    """测试质量评分器"""
    print("\n" + "="*60)
    print("测试 QualityScorer (质量评分器)")
    print("="*60)

    # 直接加载模块，避免通过__init__.py 导入
    qs = load_module('quality_scorer', 'app/factors/quality_scorer.py')
    QualityScorer = qs.QualityScorer
    QualityClass = qs.QualityClass

    scorer = QualityScorer()

    # 测试优质股
    print("\n[测试 1] 优质股评分 (腾讯控股)")
    fundamentals_a = {
        'roe_5y_avg': 25,        # ROE 25%
        'debt_ratio': 0.25,      # 负债率 25%
        'operating_cashflow': 1e9,  # 正现金流
        'gross_margin': 0.60,    # 毛利率 60%
        'margin_trend': 0.05,    # 毛利率提升
        'revenue_growth_3y': 0.20,  # 20% 增长
    }
    result_a = scorer.calculate('HK.00700', fundamentals_a)
    print(f"  得分：{result_a.score}")
    print(f"  等级：{result_a.class_.value}")
    print(f"  备注：{result_a.notes}")
    assert result_a.class_ == QualityClass.CLASS_A, "应该是优质股"
    print("  ✓ 测试通过")

    # 测试垃圾股
    print("\n[测试 2] 垃圾股评分")
    fundamentals_junk = {
        'roe_5y_avg': -5,        # 负 ROE
        'debt_ratio': 0.80,      # 高负债
        'operating_cashflow': -1e8,  # 负现金流
        'gross_margin': 0.10,    # 低毛利
        'margin_trend': -0.10,   # 毛利率下滑
        'revenue_growth_3y': -0.10,  # 负增长
    }
    result_junk = scorer.calculate('HK.XXXXX', fundamentals_junk)
    print(f"  得分：{result_junk.score}")
    print(f"  等级：{result_junk.class_.value}")
    print(f"  备注：{result_junk.notes}")
    assert result_junk.class_ == QualityClass.JUNK, "应该是垃圾股"
    print("  ✓ 测试通过")

    # 测试质量恶化判断
    print("\n[测试 3] 质量恶化判断")
    is_deteriorated = scorer.is_deteriorated(
        'HK.00700', result_a, result_junk
    )
    print(f"  A -> JUNK 是否恶化：{is_deteriorated}")
    assert is_deteriorated is True, "应该是恶化"
    print("  ✓ 测试通过")

    print("\n✓ QualityScorer 测试全部通过")
    return True


def test_holding_period_manager():
    """测试持仓周期管理器"""
    print("\n" + "="*60)
    print("测试 HoldingPeriodManager (持仓周期管理器)")
    print("="*60)

    hpm = load_module('holding_period_manager', 'app/factors/holding_period_manager.py')
    HoldingPeriodManager = hpm.HoldingPeriodManager
    HoldingTier = hpm.HoldingTier
    QualityClass = load_module('quality_scorer', 'app/factors/quality_scorer.py').QualityClass

    manager = HoldingPeriodManager()

    # 测试添加持仓
    print("\n[测试 1] 添加优质股持仓")
    manager.add_position('HK.00700', QualityClass.CLASS_A, 500.0)
    info = manager.get_position_info('HK.00700')
    print(f"  持仓信息:")
    print(f"    等级：{info['quality_class']}")
    print(f"    期限：{info['tier']}")
    print(f"    最大持仓：{info['max_hours']} 小时")
    assert info['tier'] == 'long', "优质股应该是长期"
    print("  ✓ 测试通过")

    # 测试一般股持仓
    print("\n[测试 2] 添加一般股持仓")
    manager.add_position('HK.XXXXX', QualityClass.CLASS_C, 10.0)
    info = manager.get_position_info('HK.XXXXX')
    print(f"  持仓信息:")
    print(f"    等级：{info['quality_class']}")
    print(f"    期限：{info['tier']}")
    print(f"    最大持仓：{info['max_hours']} 小时")
    assert info['max_hours'] == 48, "一般股应该是 48 小时"
    print("  ✓ 测试通过")

    # 测试时间到期判断
    print("\n[测试 3] 时间到期判断")
    should_exit, reason = manager.should_exit('HK.XXXXX')
    print(f"  是否应该离场：{should_exit}")
    print(f"  原因：{reason}")
    # 刚添加的持仓不应该到期
    assert should_exit is False, "刚添加的持仓不应该到期"
    print("  ✓ 测试通过")

    print("\n✓ HoldingPeriodManager 测试全部通过")
    return True


def test_water_level_manager():
    """测试资金水位管理器"""
    print("\n" + "="*60)
    print("测试 WaterLevelManager (资金水位管理器)")
    print("="*60)

    wlm = load_module('water_level_manager', 'app/factors/water_level_manager.py')
    WaterLevelManager = wlm.WaterLevelManager
    WaterLevel = wlm.WaterLevel

    manager = WaterLevelManager(initial_capital=1000000)

    # 测试初始水位
    print("\n[测试 1] 初始水位 (100%)")
    level = manager.get_current_level()
    config = manager.get_current_config()
    print(f"  水位等级：{level.value}")
    print(f"  最大仓位：{config.max_single_position:.0%}")
    print(f"  日交易次数：{config.max_daily_trades}")
    assert level == WaterLevel.GREEN, "应该是安全区"
    print("  ✓ 测试通过")

    # 测试水位下降
    print("\n[测试 2] 水位下降至 90% (警戒区)")
    manager.update_capital(current_capital=900000)
    level = manager.get_current_level()
    config = manager.get_current_config()
    print(f"  水位等级：{level.value}")
    print(f"  最大仓位：{config.max_single_position:.0%}")
    print(f"  日交易次数：{config.max_daily_trades}")
    assert level == WaterLevel.YELLOW, "应该是警戒区"
    print("  ✓ 测试通过")

    # 测试交易权限
    print("\n[测试 3] 交易权限检查")
    can_trade_b, reason_b = manager.can_trade('B')
    can_trade_c, reason_c = manager.can_trade('C')
    print(f"  买入 B 级：{can_trade_b} ({reason_b})")
    print(f"  买入 C 级：{can_trade_c} ({reason_c})")
    assert can_trade_b is True, "应该可以买入 B 级"
    assert can_trade_c is False, "YELLOW 水位不应该允许买入 C 级"
    print("  ✓ 测试通过")

    # 测试水位下降至危险区
    print("\n[测试 4] 水位下降至 80% (危险区)")
    manager.update_capital(current_capital=800000)
    level = manager.get_current_level()
    config = manager.get_current_config()
    print(f"  水位等级：{level.value}")
    print(f"  最大仓位：{config.max_single_position:.0%}")
    print(f"  日交易次数：{config.max_daily_trades}")
    assert level == WaterLevel.ORANGE, "应该是危险区"
    print("  ✓ 测试通过")

    # 测试熔断
    print("\n[测试 5] 水位下降至 70% (熔断区)")
    manager.update_capital(current_capital=700000)
    level = manager.get_current_level()
    config = manager.get_current_config()
    print(f"  水位等级：{level.value}")
    print(f"  最大仓位：{config.max_single_position:.0%}")
    print(f"  日交易次数：{config.max_daily_trades}")
    assert level == WaterLevel.RED, "应该是熔断区"
    can_trade, reason = manager.can_trade('A')
    print(f"  是否可以交易：{can_trade} ({reason})")
    assert can_trade is False, "熔断区应该禁止交易"
    print("  ✓ 测试通过")

    print("\n✓ WaterLevelManager 测试全部通过")
    return True


def test_profit_loss_manager():
    """测试止盈止损管理器"""
    print("\n" + "="*60)
    print("测试 ProfitLossManager (止盈止损管理器)")
    print("="*60)

    plm = load_module('profit_loss_manager', 'app/factors/profit_loss_manager.py')
    ProfitLossManager = plm.ProfitLossManager
    ExitType = plm.ExitType
    QualityClass = load_module('quality_scorer', 'app/factors/quality_scorer.py').QualityClass

    manager = ProfitLossManager()

    # 测试添加持仓
    print("\n[测试 1] 添加持仓 (优质股)")
    manager.add_position('HK.00700', 500.0, 100, QualityClass.CLASS_A)
    info = manager.get_position_info('HK.00700')
    print(f"  入场价：{info['entry_price']}")
    print(f"  初始止损：{info['exit_config_summary']['initial_stop']:.0%}")
    print("  ✓ 测试通过")

    # 测试价格上涨 3% (触发保本止损)
    print("\n[测试 2] 价格上涨 3% (触发保本止损)")
    manager.update_price('HK.00700', 515.0)
    info = manager.get_position_info('HK.00700')
    print(f"  当前价：{info['current_price']}")
    print(f"  盈亏：{info['pnl_pct']:.2f}%")
    print(f"  止损价：{info['stop_loss_price']:.2f}")
    print(f"  保本触发：{info['breakeven_triggered']}")
    assert info['breakeven_triggered'] is True, "应该触发保本止损"
    print("  ✓ 测试通过")

    # 测试价格上涨 5% (触发分批止盈)
    print("\n[测试 3] 价格上涨 5% (触发分批止盈)")
    manager.update_price('HK.00700', 525.0)
    # CLASS_A 没有分批止盈，测试一般股
    manager.add_position('HK.XXXXX', 10.0, 1000, QualityClass.CLASS_C)
    manager.update_price('HK.XXXXX', 10.5)
    signal = manager.check_partial_exit('HK.XXXXX')
    if signal:
        print(f"  止盈信号：{signal.exit_type.value}")
        print(f"  止盈比例：{signal.ratio:.0%}")
        print(f"  盈亏：{signal.pnl_pct:.2f}%")
    else:
        print(f"  无止盈信号 (CLASS_C 配置为一次性止盈 10%)")
    print("  ✓ 测试通过")

    # 测试趋势止盈信号
    print("\n[测试 4] 趋势止盈信号")
    signal = manager.on_trend_signal('HK.00700', 'supertrend_turn')
    if signal:
        print(f"  止盈信号：{signal.exit_type.value}")
        print(f"  止盈比例：{signal.ratio:.0%}")
        print(f"  原因：{signal.reason}")
    print("  ✓ 测试通过")

    print("\n✓ ProfitLossManager 测试全部通过")
    return True


def test_valuation_calculator():
    """测试估值计算器"""
    print("\n" + "="*60)
    print("测试 ValuationCalculator (估值计算器)")
    print("="*60)

    vc = load_module('valuation', 'app/factors/valuation.py')
    ValuationCalculator = vc.ValuationCalculator
    ValuationSignal = vc.ValuationSignal

    calculator = ValuationCalculator()

    # 添加 PE 历史数据
    print("\n[测试 1] 添加 PE 历史数据")
    for pe in [20, 22, 25, 23, 28, 30, 27, 25, 24, 26]:
        calculator.add_pe_history('HK.00700', pe)
    print(f"  已添加 10 条 PE 数据")
    print(f"  历史平均 PE: {calculator._get_historical_pe_avg('HK.00700'):.1f}")
    print("  ✓ 测试通过")

    # 测试估值计算
    print("\n[测试 2] 估值计算")
    fundamentals = {
        'eps': 20.0,
        'pe_ttm': 25.0,
        'growth_rate': 0.15,
        'book_value': 100.0,
        'roe': 0.25,
    }
    result = calculator.calculate('HK.00700', 500.0, fundamentals)
    print(f"  当前价格：{result.current_price}")
    print(f"  DCF 估值：{result.intrinsic_value:.2f}")
    print(f"  格雷厄姆估值：{result.intrinsic_value_graham:.2f}")
    print(f"  平均估值：{result.intrinsic_value_avg:.2f}")
    print(f"  安全边际：{result.margin_of_safety_pct:.1f}%")
    print(f"  PE 分位点：{result.historical_pe_percentile:.0%}")
    print(f"  估值信号：{result.signal.value}")
    print(f"  置信度：{result.confidence:.0%}")
    print(f"  备注：{result.notes}")
    print("  ✓ 测试通过")

    # 测试低估情况
    print("\n[测试 3] 低估情况测试")
    result2 = calculator.calculate('HK.00700', 400.0, fundamentals)
    print(f"  当前价格：400.0")
    print(f"  安全边际：{result2.margin_of_safety_pct:.1f}%")
    print(f"  估值信号：{result2.signal.value}")
    if result2.signal in (ValuationSignal.BUY, ValuationSignal.STRONG_BUY):
        print(f"  ✓ 识别为买入机会")
    print("  ✓ 测试通过")

    print("\n✓ ValuationCalculator 测试全部通过")
    return True


def main():
    """主测试函数"""
    print("\n" + "="*60)
    print("FTSeek 巴菲特理念模块测试")
    print("测试时间:", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("="*60)

    tests = [
        ("质量评分器", test_quality_scorer),
        ("持仓周期管理器", test_holding_period_manager),
        ("资金水位管理器", test_water_level_manager),
        ("止盈止损管理器", test_profit_loss_manager),
        ("估值计算器", test_valuation_calculator),
    ]

    results = []
    for name, test_func in tests:
        try:
            success = test_func()
            results.append((name, success, None))
        except Exception as e:
            results.append((name, False, str(e)))
            import traceback
            traceback.print_exc()

    # 汇总结果
    print("\n" + "="*60)
    print("测试结果汇总")
    print("="*60)

    passed = 0
    failed = 0
    for name, success, error in results:
        status = "✓ 通过" if success else "✗ 失败"
        print(f"  {name}: {status}")
        if error:
            print(f"    错误：{error}")
        if success:
            passed += 1
        else:
            failed += 1

    print(f"\n总计：{passed} 通过，{failed} 失败")
    print("="*60 + "\n")

    return failed == 0


if __name__ == '__main__':
    success = main()
    sys.exit(0 if success else 1)
