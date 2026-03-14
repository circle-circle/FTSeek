#!/usr/bin/env python3.8
"""
参数优化示例脚本

用法:
    cd /opt/ftseek/FTSeek
    python3.8 optimize_example.py
"""
import sys
import os

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.backtest.optimizer import ParameterOptimizer, optimize_strategy
from app.backtest.data_loader import generate_sample_data
from app.strategy.rsi_strategy import RSIStrategy
from app.strategy.macd_strategy import MACDStrategy
from app.strategy.confluence_v2 import ConfluenceStrategyV2


def run_rsi_optimization():
    """RSI 策略参数优化示例"""
    print("\n" + "="*80)
    print("  RSI 策略参数优化 - 网格搜索")
    print("="*80 + "\n")

    # 1. 生成数据
    print("生成测试数据...")
    data = {"HK.00700": generate_sample_data("HK.00700", start_price=380.0, days=252)}
    print(f"数据量：{len(data['HK.00700'])} 条 K 线\n")

    # 2. 定义参数网格
    param_grid = {
        "period": [10, 14, 18, 21],
        "overbought": [65, 70, 75],
        "oversold": [25, 30, 35],
    }

    print(f"参数网格:")
    for param, values in param_grid.items():
        print(f"  {param}: {values}")
    print(f"总组合数：{len(param_grid['period']) * len(param_grid['overbought']) * len(param_grid['oversold'])}\n")

    # 3. 运行优化
    print("开始网格搜索优化...\n")
    
    result = optimize_strategy(
        strategy_class=RSIStrategy,
        data=data,
        param_grid=param_grid,
        base_config={"enabled": True},
        score_func="sharpe_ratio",  # 优化目标：夏普比率
    )

    # 4. 输出结果
    print(result.summary())

    # 5. 参数敏感性分析
    if result.param_sensitivity:
        print("\n参数敏感性分析:")
        print("-" * 80)
        for param_name, sensitivity in result.param_sensitivity.items():
            print(f"\n{param_name}:")
            for value, score in sensitivity:
                print(f"  {value:>8} -> 平均得分={score:.4f}")
        print("-" * 80)

    return result


def run_macd_optimization():
    """MACD 策略参数优化示例"""
    print("\n" + "="*80)
    print("  MACD 策略参数优化 - 随机搜索")
    print("="*80 + "\n")

    # 生成数据
    data = {"HK.00700": generate_sample_data("HK.00700", start_price=380.0, days=252)}

    # 创建优化器
    optimizer = ParameterOptimizer(
        strategy_class=MACDStrategy,
        data=data,
        base_config={"enabled": True},
    )

    # 参数分布
    param_distributions = {
        "fast_period": (8, 16),
        "slow_period": (20, 30),
        "signal_period": (7, 12),
    }

    print("开始随机搜索优化...")
    print(f"参数分布:")
    for param, (min_val, max_val) in param_distributions.items():
        print(f"  {param}: [{min_val}, {max_val}]")
    print()

    # 运行随机搜索
    result = optimizer.random_search(
        param_distributions=param_distributions,
        n_iter=30,  # 30 次迭代
        score_func="sharpe_ratio",
        show_progress=True,
    )

    print(result.summary())

    return result


def run_walk_forward_analysis():
    """Walk-Forward 分析示例"""
    print("\n" + "="*80)
    print("  Walk-Forward 分析 - 滚动窗口优化")
    print("="*80 + "\n")

    # 生成更长的数据序列
    data = {"HK.00700": generate_sample_data("HK.00700", start_price=380.0, days=500)}

    # 参数网格
    param_grid = {
        "period": [12, 14, 16],
        "overbought": [68, 70, 72],
        "oversold": [28, 30, 32],
    }

    print("开始 Walk-Forward 分析...")
    print(f"训练期：60 天，测试期：20 天\n")

    optimizer = ParameterOptimizer(
        strategy_class=RSIStrategy,
        data=data,
        base_config={"enabled": True},
    )

    wf_result = optimizer.walk_forward(
        param_grid=param_grid,
        train_periods=60,
        test_periods=20,
        score_func="sharpe_ratio",
    )

    # 输出结果
    print("\n" + "="*80)
    print("  Walk-Forward 分析结果")
    print("="*80)
    print(f"窗口数量：{wf_result['n_windows']}")
    print(f"平均训练得分：{wf_result['avg_train_score']:.4f}")
    print(f"平均测试得分：{wf_result['avg_test_score']:.4f}")
    
    # 检查过拟合
    if wf_result['avg_train_score'] > 0 and wf_result['avg_test_score'] > 0:
        overfitting_ratio = wf_result['avg_train_score'] / wf_result['avg_test_score']
        print(f"过拟合比率：{overfitting_ratio:.2f}x")
        
        if overfitting_ratio > 2.0:
            print("⚠️  警告：可能存在过拟合！")
        elif overfitting_ratio > 1.5:
            print("⚠️  注意：训练和测试表现有一定差距")
        else:
            print("✅ 训练和测试表现一致，无明显过拟合")
    
    print("="*80 + "\n")

    return wf_result


def run_confluence_v2_optimization():
    """Confluence V2 策略优化 (带市场状态识别)"""
    print("\n" + "="*80)
    print("  Confluence V2 策略优化 - 市场状态增强版")
    print("="*80 + "\n")

    # 生成数据
    data = {"HK.00700": generate_sample_data("HK.00700", start_price=380.0, days=252)}

    # 参数网格
    param_grid = {
        "min_factors": [2, 3, 4],
        "use_market_regime": [True, False],
        "use_dynamic_weights": [True, False],
    }

    print("优化 Confluence V2 策略...")
    print(f"测试配置:")
    print(f"  - 最小因子数：[2, 3, 4]")
    print(f"  - 市场状态识别：[启用，禁用]")
    print(f"  - 动态权重：[启用，禁用]\n")

    result = optimize_strategy(
        strategy_class=ConfluenceStrategyV2,
        data=data,
        param_grid=param_grid,
        base_config={"enabled": True},
        score_func="sharpe_ratio",
    )

    print(result.summary())

    # 分析市场状态的效果
    print("\n市场状态识别效果分析:")
    print("-" * 80)
    
    with_regime_results = [r for r in result.all_results if r["params"].get("use_market_regime", False)]
    without_regime_results = [r for r in result.all_results if not r["params"].get("use_market_regime", False)]
    
    if with_regime_results and without_regime_results:
        avg_with = sum(r["score"] for r in with_regime_results) / len(with_regime_results)
        avg_without = sum(r["score"] for r in without_regime_results) / len(without_regime_results)
        
        improvement = (avg_with - avg_without) / avg_without * 100 if avg_without > 0 else 0
        
        print(f"启用市场状态识别平均得分：{avg_with:.4f}")
        print(f"禁用市场状态识别平均得分：{avg_without:.4f}")
        print(f"提升：{improvement:+.2f}%")
        
        if improvement > 5:
            print("✅ 市场状态识别显著提升策略表现")
        elif improvement > 0:
            print("👍 市场状态识别有一定帮助")
        else:
            print("⚠️  市场状态识别未显示明显优势")
    
    print("-" * 80 + "\n")

    return result


def main():
    """主函数"""
    print("\n" + "="*80)
    print("  FTSeek 参数优化示例")
    print("="*80)

    # 示例 1: RSI 网格搜索
    rsi_result = run_rsi_optimization()

    # 示例 2: MACD 随机搜索
    macd_result = run_macd_optimization()

    # 示例 3: Walk-Forward 分析
    wf_result = run_walk_forward_analysis()

    # 示例 4: Confluence V2 优化
    confluence_result = run_confluence_v2_optimization()

    # 总结
    print("\n" + "="*80)
    print("  优化完成!")
    print("="*80)
    print("\n关键发现:")
    print(f"1. RSI 最优参数：{rsi_result.best_params}")
    print(f"2. MACD 最优得分：{macd_result.best_score:.4f}")
    print(f"3. Walk-Forward 测试得分：{wf_result.get('avg_test_score', 0):.4f}")
    print(f"4. Confluence V2 市场状态提升：见上方分析")
    print("\n下一步:")
    print("1. 使用最优参数进行实盘测试")
    print("2. 定期重新优化以适应市场变化")
    print("3. 监控实盘表现与回测的差异")
    print("\n")


if __name__ == "__main__":
    main()
