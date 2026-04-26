#!/usr/bin/env python3
"""
回测验证测试

测试内容:
1. StandardStrategy 基类功能
2. 回测引擎基本功能
3. 示例策略回测
4. 参数优化
5. 绩效指标计算

使用方法:
  cd /opt/ftseek/FTSeek
  python scripts/test_backtest.py
"""

import sys
import os
import logging
import numpy as np
import pandas as pd

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger("test_backtest")


def generate_test_data(bars: int = 500, seed: int = 42) -> pd.DataFrame:
    """生成模拟 OHLCV 数据"""
    np.random.seed(seed)
    
    dates = pd.date_range(start='2025-01-01', periods=bars, freq='1h')
    
    # 几何布朗运动生成价格
    returns = np.random.normal(0.0001, 0.01, bars)
    close = 100 * np.exp(np.cumsum(returns))
    
    # 生成 OHLCV
    high = close * (1 + np.abs(np.random.normal(0, 0.005, bars)))
    low = close * (1 - np.abs(np.random.normal(0, 0.005, bars)))
    open_price = close * (1 + np.random.normal(0, 0.002, bars))
    volume = np.random.uniform(1000000, 5000000, bars)
    
    df = pd.DataFrame({
        'open': open_price,
        'high': high,
        'low': low,
        'close': close,
        'volume': volume,
    }, index=dates)
    
    return df


class TestResult:
    def __init__(self):
        self.checks = []
    
    def add(self, name: str, passed: bool, message: str = ""):
        self.checks.append({"name": name, "passed": passed, "message": message})
        icon = "✅" if passed else "❌"
        logger.info(f"  {icon} {name}: {message}")
    
    def is_healthy(self) -> bool:
        return all(c['passed'] for c in self.checks)
    
    def summary(self) -> str:
        passed = sum(1 for c in self.checks if c['passed'])
        total = len(self.checks)
        return f"{passed}/{total} 通过"


def test_standard_strategy():
    """测试 StandardStrategy 基类"""
    logger.info("=" * 60)
    logger.info("1. StandardStrategy 基类测试")
    logger.info("=" * 60)
    
    result = TestResult()
    
    try:
        from app.strategy.standard_strategy import StandardStrategy
        from app.strategy.example_strategies import RSIMeanReversion, MACross, MACDMomentum
        
        # 测试实例化
        strategy = RSIMeanReversion()
        result.add("实例化", True, f"name={strategy.name}")
        
        # 测试元数据
        result.add("元数据", strategy.name == "rsi_mean_reversion", strategy.name)
        
        # 测试配置覆盖
        strategy2 = RSIMeanReversion(config={'rsi_period': 20, 'stoploss': -0.05})
        result.add("配置覆盖", strategy2.rsi_period == 20, f"rsi_period={strategy2.rsi_period}")
        
        # 测试分析流程
        df = generate_test_data(100)
        analyzed = strategy.analyze_pair(df, {'pair': 'TEST', 'timeframe': '1h'})
        
        result.add("分析流程", 'rsi' in analyzed.columns, f"指标列: {[c for c in analyzed.columns if c not in df.columns]}")
        result.add("入场信号", 'enter_long' in analyzed.columns, f"信号列: {analyzed['enter_long'].sum()} 个")
        result.add("出场信号", 'exit_long' in analyzed.columns, f"信号列: {analyzed['exit_long'].sum()} 个")
        
        # 测试其他策略
        for cls in [MACross, MACDMomentum]:
            s = cls()
            df2 = generate_test_data(100)
            analyzed2 = s.analyze_pair(df2, {})
            result.add(
                f"{s.name} 分析",
                'enter_long' in analyzed2.columns and 'exit_long' in analyzed2.columns,
                f"entry={analyzed2['enter_long'].sum()}, exit={analyzed2['exit_long'].sum()}"
            )
        
        # 测试参数网格
        grid = strategy.get_param_grid()
        result.add("参数网格", len(grid) > 0, f"{len(grid)} 个参数")
        
    except Exception as e:
        result.add("StandardStrategy", False, str(e))
        import traceback
        logger.error(f"异常: {traceback.format_exc()}")
    
    return result


def test_backtest_engine():
    """测试回测引擎"""
    logger.info("\n" + "=" * 60)
    logger.info("2. 回测引擎测试")
    logger.info("=" * 60)
    
    result = TestResult()
    
    try:
        from app.backtest.engine import get_backtest_engine, BacktestEngine
        from app.strategy.example_strategies import RSIMeanReversion
        
        # 测试实例化
        engine = get_backtest_engine(
            initial_capital=1_000_000,
            fee_pct=0.001,
            slippage_pct=0.001,
            max_open_trades=3,
            position_size_pct=0.10,
        )
        result.add("实例化", True, f"initial_capital={engine.initial_capital}")
        
        # 生成测试数据
        df = generate_test_data(500)
        result.add("测试数据", len(df) == 500, f"{len(df)} bars")
        
        # 运行回测
        strategy = RSIMeanReversion()
        bt_result = engine.run(strategy, df, pair='TEST')
        
        result.add("回测完成", bt_result.final_capital > 0, f"${bt_result.final_capital:,.2f}")
        result.add("总收益率", isinstance(bt_result.total_return_pct, float), f"{bt_result.total_return_pct:.2%}")
        result.add("夏普比率", isinstance(bt_result.sharpe_ratio, float), f"{bt_result.sharpe_ratio:.3f}")
        result.add("最大回撤", 0 <= bt_result.max_drawdown_pct <= 1, f"{bt_result.max_drawdown_pct:.2%}")
        result.add("交易数", bt_result.total_trades >= 0, f"{bt_result.total_trades} 笔")
        
        # 测试结果输出
        summary = bt_result.summary()
        result.add("摘要输出", len(summary) > 100, f"{len(summary)} 字符")
        
        # 测试字典转换
        result_dict = bt_result.to_dict()
        result.add("字典转换", 'sharpe_ratio' in result_dict, f"键数: {len(result_dict)}")
        
    except Exception as e:
        result.add("BacktestEngine", False, str(e))
        import traceback
        logger.error(f"异常: {traceback.format_exc()}")
    
    return result


def test_parameter_optimization():
    """测试参数优化"""
    logger.info("\n" + "=" * 60)
    logger.info("3. 参数优化测试")
    logger.info("=" * 60)
    
    result = TestResult()
    
    try:
        from app.backtest.engine import get_backtest_engine
        from app.strategy.example_strategies import RSIMeanReversion
        
        engine = get_backtest_engine()
        strategy = RSIMeanReversion()
        
        # 小网格测试
        small_grid = {
            'rsi_period': [10, 14],
            'stoploss': [-0.04, -0.06],
        }
        
        df = generate_test_data(200)
        
        opt_result = engine.optimize(
            strategy=strategy,
            dataframe=df,
            pair='TEST',
            param_grid=small_grid,
            metric='sharpe_ratio',
        )
        
        result.add("优化完成", opt_result['best_params'] is not None, 
                  f"最优参数: {opt_result['best_params']}")
        result.add("组合数", opt_result['total_combinations'] == 4, 
                  f"{opt_result['total_combinations']} 组")
        result.add("最优结果", opt_result['best_result'] is not None,
                  f"Sharpe: {opt_result['best_result'].sharpe_ratio:.3f}")
        
    except Exception as e:
        result.add("参数优化", False, str(e))
        import traceback
        logger.error(f"异常: {traceback.format_exc()}")
    
    return result


def test_walk_forward():
    """测试 Walk-Forward 验证"""
    logger.info("\n" + "=" * 60)
    logger.info("4. Walk-Forward 验证测试")
    logger.info("=" * 60)
    
    result = TestResult()
    
    try:
        from app.backtest.engine import get_backtest_engine
        from app.strategy.example_strategies import RSIMeanReversion
        
        engine = get_backtest_engine()
        strategy = RSIMeanReversion()
        
        # 生成足够长的数据
        df = generate_test_data(500)
        
        wf_result = engine.walk_forward(
            strategy=strategy,
            dataframe=df,
            train_bars=100,
            test_bars=50,
            pair='TEST',
        )
        
        result.add("Walk-Forward 完成", wf_result['num_windows'] > 0,
                  f"{wf_result['num_windows']} 窗口")
        result.add("平均 Sharpe", isinstance(wf_result['avg_sharpe'], float),
                  f"{wf_result['avg_sharpe']:.3f}")
        result.add("稳定性", isinstance(wf_result['stability'], float),
                  f"{wf_result['stability']:.3f}")
        
    except Exception as e:
        result.add("Walk-Forward", False, str(e))
        import traceback
        logger.error(f"异常: {traceback.format_exc()}")
    
    return result


def test_strategy_compatibility():
    """测试策略兼容性 (与现有 BaseStrategy 共存)"""
    logger.info("\n" + "=" * 60)
    logger.info("5. 策略兼容性测试")
    logger.info("=" * 60)
    
    result = TestResult()
    
    try:
        # 测试现有 BaseStrategy
        from app.strategy.base import BaseStrategy, Signal, SignalAction
        from app.strategy.confluence_v2 import ConfluenceStrategyV2
        
        # 测试现有策略仍然可用
        confluence = ConfluenceStrategyV2()
        result.add("ConfluenceV2 加载", confluence.name == "confluence_v2", confluence.name)
        
        # 测试新策略
        from app.strategy.standard_strategy import StandardStrategy
        from app.strategy.example_strategies import RSIMeanReversion
        
        rsi = RSIMeanReversion()
        result.add("RSI 策略加载", rsi.name == "rsi_mean_reversion", rsi.name)
        
        # 确认两者共存
        result.add("策略共存", True, "BaseStrategy + StandardStrategy 共存")
        
    except Exception as e:
        result.add("兼容性", False, str(e))
        import traceback
        logger.error(f"异常: {traceback.format_exc()}")
    
    return result


def main():
    logger.info("=" * 60)
    logger.info("FTSeek 回测验证测试")
    logger.info(f"时间: {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)
    
    all_results = []
    
    # 1. StandardStrategy
    all_results.append(test_standard_strategy())
    
    # 2. BacktestEngine
    all_results.append(test_backtest_engine())
    
    # 3. 参数优化
    all_results.append(test_parameter_optimization())
    
    # 4. Walk-Forward
    all_results.append(test_walk_forward())
    
    # 5. 兼容性
    all_results.append(test_strategy_compatibility())
    
    # 总结
    logger.info("\n" + "=" * 60)
    logger.info("测试总结")
    logger.info("=" * 60)
    
    total_passed = 0
    total_checks = 0
    
    for i, r in enumerate(all_results, 1):
        passed = sum(1 for c in r.checks if c['passed'])
        total = len(r.checks)
        total_passed += passed
        total_checks += total
        status = "✅" if r.is_healthy() else "⚠️"
        logger.info(f"  {status} 测试 {i}: {passed}/{total}")
    
    logger.info(f"\n总计: {total_passed}/{total_checks} 通过")
    logger.info(f"总体状态: {'✅ 全部通过' if total_passed == total_checks else '⚠️ 部分失败'}")
    
    return total_passed == total_checks


if __name__ == "__main__":
    try:
        success = main()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        logger.info("\n测试被用户中断")
        sys.exit(130)
    except Exception as e:
        logger.error(f"\n测试异常: {e}", exc_info=True)
        sys.exit(1)
