#!/usr/bin/env python3.8
"""
回测框架使用示例

用法:
    cd /opt/ftseek/FTSeek
    python3.8 backtest_example.py
"""
import sys
import os

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.backtest.backtester import Backtester, MultiStrategyBacktester
from app.backtest.data_loader import generate_sample_data
from app.strategy.rsi_strategy import RSIStrategy
from app.strategy.macd_strategy import MACDStrategy
from app.strategy.supertrend_strategy import SuperTrendStrategy
from app.strategy.confluence_strategy import ConfluenceStrategy


def run_single_strategy_backtest():
    """运行单策略回测示例"""
    print("\n" + "="*80)
    print("  单策略回测示例 - RSI 策略")
    print("="*80 + "\n")

    # 1. 生成模拟数据 (实际使用时从 CSV 加载)
    print("生成模拟数据...")
    data = {
        "HK.00700": generate_sample_data("HK.00700", start_price=380.0, days=252),
    }
    print(f"数据范围：{data['HK.00700'][0]['time_key']} 至 {data['HK.00700'][-1]['time_key']}")
    print(f"数据量：{len(data['HK.00700'])} 条 K 线\n")

    # 2. 创建策略
    strategy_config = {
        "enabled": True,
        "period": 14,
        "overbought": 70,
        "oversold": 30,
    }
    strategy = RSIStrategy(strategy_config)

    # 3. 创建回测器
    backtester = Backtester(
        strategy=strategy,
        data=data,
        initial_capital=100000.0,
        fee_rate=0.0003,  # 万三
        slippage=0.001,   # 千一
        risk_per_trade=0.10,  # 每次交易使用 10% 资金
    )

    # 4. 运行回测
    result = backtester.run(show_progress=True)

    # 5. 输出结果
    print("\n" + result.metrics.summary())

    # 6. 显示部分交易记录
    if result.trades:
        print("\n最近 5 笔交易:")
        print("-" * 80)
        for trade in result.trades[-5:]:
            print(f"{trade.code} | {trade.entry_time[:10]} | {trade.direction:<4} | "
                  f"盈亏：{trade.pnl:>10.2f} ({trade.pnl_percent:>7.2%})")
        print("-" * 80)

    return result


def run_multi_strategy_comparison():
    """运行多策略对比回测"""
    print("\n" + "="*80)
    print("  多策略对比回测")
    print("="*80 + "\n")

    # 生成数据
    print("生成模拟数据...")
    data = {
        "HK.00700": generate_sample_data("HK.00700", start_price=380.0, days=252),
    }

    # 创建多策略回测器
    multi_backtester = MultiStrategyBacktester(data=data, initial_capital=100000.0)

    # 添加并运行策略
    print("\n回测 RSI 策略...")
    multi_backtester.add_strategy("RSI", RSIStrategy({"period": 14, "overbought": 70, "oversold": 30}))

    print("回测 MACD 策略...")
    multi_backtester.add_strategy("MACD", MACDStrategy({"fast_period": 12, "slow_period": 26, "signal_period": 9}))

    print("回测 SuperTrend 策略...")
    multi_backtester.add_strategy("SuperTrend", SuperTrendStrategy({"atr_period": 10, "multiplier": 3.0}))

    print("回测 Confluence 策略...")
    multi_backtester.add_strategy("Confluence", ConfluenceStrategy({"min_factors": 3}))

    # 生成对比报告
    print("\n")
    report = multi_backtester.compare()
    print(report)

    return multi_backtester.results


def main():
    """主函数"""
    print("\n" + "="*80)
    print("  FTSeek 回测框架示例")
    print("="*80)

    # 示例 1: 单策略回测
    single_result = run_single_strategy_backtest()

    # 示例 2: 多策略对比
    multi_results = run_multi_strategy_comparison()

    # 总结
    print("\n" + "="*80)
    print("  回测完成!")
    print("="*80)
    print("\n提示:")
    print("1. 实际使用时，请从 DataLoader 加载真实历史数据")
    print("2. 调整策略参数进行优化")
    print("3. 使用 MultiStrategyBacktester 对比不同策略")
    print("4. 关注夏普比率、最大回撤、盈利因子等关键指标")
    print("\n")


if __name__ == "__main__":
    main()
