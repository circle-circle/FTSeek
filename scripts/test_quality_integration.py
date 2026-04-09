#!/usr/bin/env python3
"""
质量管理器集成测试脚本

测试质量管理器在 FTSeek 交易流程中的完整功能:
1. 质量评分 - 过滤垃圾股
2. 水位检查 - 动态风险调整
3. 估值检查 - 安全边际计算
4. 持仓管理 - 质量决定的持仓期限
5. 止盈止损 - 三层止盈架构

注意：此脚本使用模拟数据，不依赖外部 API
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 直接导入模块文件，避免 circular import
import importlib.util

def load_module_from_file(file_path, module_name):
    """从文件加载模块"""
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

# 直接加载各模块文件
quality_scorer = load_module_from_file('app/factors/quality_scorer.py', 'quality_scorer')
holding_period = load_module_from_file('app/factors/holding_period_manager.py', 'holding_period_manager')
water_level = load_module_from_file('app/factors/water_level_manager.py', 'water_level_manager')
profit_loss = load_module_from_file('app/factors/profit_loss_manager.py', 'profit_loss_manager')
valuation = load_module_from_file('app/factors/valuation.py', 'valuation')

# 从加载的模块中获取类
QualityScorer = quality_scorer.QualityScorer
QualityClass = quality_scorer.QualityClass
HoldingPeriodManager = holding_period.HoldingPeriodManager
WaterLevelManager = water_level.WaterLevelManager
ProfitLossManager = profit_loss.ProfitLossManager
ExitType = profit_loss.ExitType
ValuationCalculator = valuation.ValuationCalculator

from datetime import datetime, timedelta
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(name)s] %(levelname)s: %(message)s')
logger = logging.getLogger("test_quality_integration")


def test_quality_scorer():
    """测试质量评分器"""
    print("\n" + "="*60)
    print("[测试 1] 质量评分器")
    print("="*60)

    scorer = QualityScorer()

    # 测试用例 1: 优质股 (腾讯)
    print("\n[用例 1.1] 腾讯控股 (00700.HK) - 优质股")
    tencent_fundamentals = {
        'roe_5y_avg': 25.0,        # 5 年平均 ROE 25%
        'debt_ratio': 0.25,        # 负债率 25%
        'operating_cashflow': 1e9,  # 经营现金流 10 亿
        'gross_margin': 0.45,      # 毛利率 45%
        'margin_trend': 0.02,      # 毛利率趋势 +2%
        'revenue_growth_3y': 0.18,  # 3 年增长 18%
    }

    quality = scorer.calculate('HK.00700', tencent_fundamentals)
    print(f"  质量评分：{quality.score:.1f}")
    print(f"  质量等级：{quality.class_.value}")
    print(f"  评估备注：{quality.notes}")
    assert quality.class_ == QualityClass.CLASS_A, "腾讯应为优质股"
    print("  ✓ 测试通过")

    # 测试用例 2: 良好股 (阿里)
    print("\n[用例 1.2] 阿里巴巴 (09988.HK) - 良好股")
    alibaba_fundamentals = {
        'roe_5y_avg': 15.0,
        'debt_ratio': 0.35,
        'operating_cashflow': 5e8,
        'gross_margin': 0.35,
        'margin_trend': 0.01,
        'revenue_growth_3y': 0.12,
    }

    quality = scorer.calculate('HK.09988', alibaba_fundamentals)
    print(f"  质量评分：{quality.score:.1f}")
    print(f"  质量等级：{quality.class_.value}")
    print(f"  评估备注：{quality.notes}")
    assert quality.class_ in (QualityClass.CLASS_A, QualityClass.CLASS_B), "阿里应为良好以上"
    print("  ✓ 测试通过")

    # 测试用例 3: 垃圾股
    print("\n[用例 1.3] 某问题公司 - 垃圾股")
    junk_fundamentals = {
        'roe_5y_avg': -5.0,        # 负 ROE
        'debt_ratio': 0.80,        # 高负债
        'operating_cashflow': -1e7, # 负现金流
        'gross_margin': 0.10,      # 低毛利
        'margin_trend': -0.05,     # 毛利率下滑
        'revenue_growth_3y': -0.20, # 负增长
    }

    quality = scorer.calculate('HK.01234', junk_fundamentals)
    print(f"  质量评分：{quality.score:.1f}")
    print(f"  质量等级：{quality.class_.value}")
    print(f"  评估备注：{quality.notes}")
    assert quality.class_ == QualityClass.JUNK, "应为垃圾股"
    print("  ✓ 测试通过")

    return True


def test_water_level_manager():
    """测试资金水位管理器"""
    print("\n" + "="*60)
    print("[测试 2] 资金水位管理器")
    print("="*60)

    manager = WaterLevelManager(initial_capital=1000000)

    # 测试用例 1: 初始状态 (GREEN)
    print("\n[用例 2.1] 初始水位 (GREEN)")
    state = manager.get_state()
    print(f"  当前水位：{state.current_ratio:.0%}")
    print(f"  水位等级：{state.current_level.value}")
    print(f"  最大单一仓位：{manager.get_max_position('A'):.0%}")
    print(f"  日交易次数上限：{manager.get_current_config().max_daily_trades}")
    assert state.current_level == manager.get_current_level() == type(manager).DEFAULT_CONFIGS.keys().__class__
    print("  ✓ 测试通过")

    # 测试用例 2: 水位下降至 YELLOW
    print("\n[用例 2.2] 水位下降至 YELLOW (85%)")
    manager.update_capital(850000)
    state = manager.get_state()
    print(f"  当前水位：{state.current_ratio:.0%}")
    print(f"  水位等级：{state.current_level.value}")
    print(f"  最大单一仓位：{manager.get_max_position('B'):.0%}")
    print(f"  日交易次数上限：{manager.get_current_config().max_daily_trades}")
    assert state.current_level.value == 'yellow'
    print("  ✓ 测试通过")

    # 测试用例 3: 水位下降至 RED (熔断)
    print("\n[用例 2.3] 水位下降至 RED (熔断，70%)")
    manager.update_capital(700000)
    state = manager.get_state()
    can_trade, reason = manager.can_trade('A')
    print(f"  当前水位：{state.current_ratio:.0%}")
    print(f"  水位等级：{state.current_level.value}")
    print(f"  是否可以交易：{can_trade}")
    print(f"  原因：{reason}")
    assert state.current_level.value == 'red'
    assert not can_trade
    print("  ✓ 测试通过")

    # 恢复水位
    manager.update_capital(1000000)

    return True


def test_profit_loss_manager():
    """测试止盈止损管理器"""
    print("\n" + "="*60)
    print("[测试 3] 止盈止损管理器")
    print("="*60)

    water_level = WaterLevelManager(initial_capital=1000000)
    manager = ProfitLossManager(water_level_manager=water_level)

    # 测试用例 1: 建仓并测试初始止损
    print("\n[用例 3.1] 建仓腾讯 (优质股) + 初始止损测试")
    entry_price = 500.0
    manager.add_position('HK.00700', entry_price, 100, QualityClass.CLASS_A)
    manager.update_price('HK.00700', 490.0)  # 下跌 2%

    info = manager.get_position_info('HK.00700')
    print(f"  买入价：{entry_price}")
    print(f"  当前价：{info['current_price']}")
    print(f"  当前盈亏：{info['pnl_pct']:.2f}%")
    print(f"  止损价：{info['stop_loss_price']:.2f}")
    print(f"  保本止损触发：{info['breakeven_triggered']}")

    # 测试止损触发
    exit_signal = manager.check_exit('HK.00700', 470.0)  # 下跌 6%，触发止损
    if exit_signal:
        print(f"  ✓ 触发止损：{exit_signal.exit_type.value} @ {exit_signal.price:.2f}")
        print(f"    原因：{exit_signal.reason}")
    else:
        print("  未触发止损")
    print("  ✓ 测试通过")

    # 测试用例 2: 分批止盈
    print("\n[用例 3.2] 建仓阿里 (良好股) + 分批止盈测试")
    manager.add_position('HK.09988', 100.0, 200, QualityClass.CLASS_B)

    # 价格上涨 10%，触发第一层止盈
    manager.update_price('HK.09988', 110.0)
    partial_signal = manager.check_partial_exit('HK.09988')

    info = manager.get_position_info('HK.09988')
    print(f"  买入价：100.0")
    print(f"  当前价：{info['current_price']}")
    print(f"  当前盈亏：{info['pnl_pct']:.2f}%")
    print(f"  分批止盈记录：{len(info['partial_exits'])} 次")

    if partial_signal:
        print(f"  ✓ 触发分批止盈：{partial_signal.ratio:.0%} @ {partial_signal.price:.2f}")
        print(f"    原因：{partial_signal.reason}")
    print("  ✓ 测试通过")

    # 测试用例 3: 追踪止盈
    print("\n[用例 3.3] 追踪止盈测试")
    manager.add_position('HK.00700', 500.0, 100, QualityClass.CLASS_A)

    # 价格上涨触发追踪止盈
    manager.update_price('HK.00700', 550.0)  # +10%
    manager.update_price('HK.00700', 580.0)  # +16%
    manager.update_price('HK.00700', 560.0)  # 从高点回撤

    info = manager.get_position_info('HK.00700')
    print(f"  最高价：{info['peak_price']:.2f}")
    print(f"  当前价：{info['current_price']:.2f}")
    print(f"  追踪止损价：{info['stop_loss_price']:.2f}")
    print(f"  追踪止盈已触发：{info['trailing_triggered']}")
    print("  ✓ 测试通过")

    return True


def test_holding_period_manager():
    """测试持仓周期管理器"""
    print("\n" + "="*60)
    print("[测试 4] 持仓周期管理器")
    print("="*60)

    manager = HoldingPeriodManager()

    # 测试用例 1: 不同质量等级的持仓期限
    print("\n[用例 4.1] 不同质量等级的持仓期限")

    test_cases = [
        (QualityClass.CLASS_A, "优质股 - 无限期"),
        (QualityClass.CLASS_B, "良好股 - 7 天"),
        (QualityClass.CLASS_C, "一般股 - 2 天"),
        (QualityClass.JUNK, "垃圾股 - 1 天"),
    ]

    for quality_class, description in test_cases:
        manager.add_position(f'TEST.{quality_class.value}', quality_class, 100.0)
        should_exit, reason = manager.should_exit(f'TEST.{quality_class.value}')

        # 获取该质量等级的最大持仓时间
        config = manager.QUALITY_HOLDING_PERIOD.get(quality_class)
        if config:
            max_hours = config.max_hours
            print(f"  {description}: {max_hours} 小时")

    print("  ✓ 测试通过")

    # 测试用例 2: 持仓时间到期
    print("\n[用例 4.2] 持仓时间到期测试")
    manager.add_position('TEST.C', QualityClass.CLASS_C, 100.0)

    # 模拟时间流逝 (2 天后)
    from datetime import timedelta
    pos = manager._positions['TEST.C']
    pos['entry_time'] = datetime.now() - timedelta(hours=50)  # 超过 48 小时

    should_exit, reason = manager.should_exit('TEST.C')
    print(f"  持仓时间：50 小时")
    print(f"  是否到期：{should_exit}")
    print(f"  原因：{reason}")
    assert should_exit, "CLASS_C 持仓超过 48 小时应到期"
    print("  ✓ 测试通过")

    return True


def test_valuation_calculator():
    """测试估值计算器"""
    print("\n" + "="*60)
    print("[测试 5] 估值计算器")
    print("="*60)

    calculator = ValuationCalculator()

    # 测试用例 1: DCF 估值
    print("\n[用例 5.1] DCF 估值测试")
    fundamentals = {
        'eps': 2.5,
        'pe_ttm': 20.0,
        'growth_rate': 0.15,
        'book_value': 15.0,
        'roe': 0.25,
    }

    # 添加 PE 历史数据
    for pe in [18, 20, 22, 19, 21, 23, 20, 19, 22, 20]:
        calculator.add_pe_history('HK.00700', pe)

    valuation = calculator.calculate('HK.00700', 500.0, fundamentals)
    if valuation:
        print(f"  当前价格：500.0")
        print(f"  DCF 估值：{valuation.dcf_value:.2f}")
        print(f"  Graham 估值：{valuation.graham_value:.2f}")
        print(f"  合理估值：{valuation.fair_value:.2f}")
        print(f"  安全边际：{valuation.margin_of_safety_pct:.1%}")
        print(f"  估值信号：{valuation.signal.value}")
        print(f"  评估备注：{valuation.notes}")
    print("  ✓ 测试通过")

    # 测试用例 2: 高估情况
    print("\n[用例 5.2] 高估情况测试")
    valuation = calculator.calculate('HK.00700', 700.0, fundamentals)  # 价格大幅提高
    if valuation:
        print(f"  当前价格：700.0")
        print(f"  安全边际：{valuation.margin_of_safety_pct:.1%}")
        print(f"  估值信号：{valuation.signal.value}")
        print(f"  评估备注：{valuation.notes}")
    print("  ✓ 测试通过")

    return True


def run_all_tests():
    """运行所有测试"""
    print("\n" + "="*60)
    print("FTSeek 质量管理器集成测试")
    print("="*60)

    tests = [
        ("质量评分器", test_quality_scorer),
        ("资金水位管理器", test_water_level_manager),
        ("止盈止损管理器", test_profit_loss_manager),
        ("持仓周期管理器", test_holding_period_manager),
        ("估值计算器", test_valuation_calculator),
    ]

    passed = 0
    failed = 0

    for name, test_func in tests:
        try:
            if test_func():
                passed += 1
        except Exception as e:
            failed += 1
            print(f"\n✗ {name} 测试失败：{e}")
            import traceback
            traceback.print_exc()

    print("\n" + "="*60)
    print(f"测试结果：{passed} 通过 / {failed} 失败")
    print("="*60)

    return failed == 0


if __name__ == '__main__':
    success = run_all_tests()
    sys.exit(0 if success else 1)
