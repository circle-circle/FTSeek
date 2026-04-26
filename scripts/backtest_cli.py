#!/usr/bin/env python3
"""
回测 CLI 工具

用法:
  # 运行单策略回测
  python scripts/backtest_cli.py run --strategy rsi_mean_reversion --pair HK.00700 --days 365

  # 参数优化
  python scripts/backtest_cli.py optimize --strategy rsi_mean_reversion --pair HK.00700 --days 180

  # 列出可用策略
  python scripts/backtest_cli.py list

  # Walk-Forward 验证
  python scripts/backtest_cli.py walkforward --strategy rsi_mean_reversion --pair HK.00700 --days 365

更新时间: 2026-04-13
"""

import sys
import os
import json
import argparse
import logging
from datetime import datetime, timedelta

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger("backtest_cli")

# 回测输出目录
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "backtest_results")
os.makedirs(OUTPUT_DIR, exist_ok=True)


def get_strategy(name: str):
    """根据名称获取策略实例"""
    from app.strategy.example_strategies import RSIMeanReversion, MACross, MACDMomentum
    
    strategies = {
        "rsi_mean_reversion": RSIMeanReversion,
        "ma_cross": MACross,
        "macd_momentum": MACDMomentum,
    }
    
    if name not in strategies:
        raise ValueError(f"未知策略: {name}. 可用: {list(strategies.keys())}")
    
    return strategies[name]()


def load_data(pair: str, days: int = 365, timeframe: str = "1h") -> 'pd.DataFrame':
    """
    加载历史数据
    
    优先级:
    1. 本地 CSV 文件 (data/backtest_data/{pair}_{timeframe}.csv)
    2. 从富途 API 获取
    """
    import pandas as pd
    
    # 尝试本地文件
    csv_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "backtest_data",
        f"{pair.replace('.', '_')}_{timeframe}.csv"
    )
    
    if os.path.exists(csv_path):
        logger.info(f"从本地文件加载: {csv_path}")
        df = pd.read_csv(csv_path, index_col=0, parse_dates=True)
        return df
    
    # 从富途 API 获取
    logger.info(f"从富途 API 获取数据: {pair} {days} 天 {timeframe}")
    try:
        from futu import OpenQuoteContext, KLType, AuType
        
        # 连接富途
        from app import config
        ctx = OpenQuoteContext(host=config.OPEND_HOST, port=config.OPEND_PORT)
        
        # 计算日期范围
        end_date = datetime.now().strftime('%Y-%m-%d')
        start_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
        
        ktype_map = {
            "1m": KLType.K_1M,
            "5m": KLType.K_5M,
            "15m": KLType.K_15M,
            "1h": KLType.K_60M,
            "1d": KLType.K_DAY,
        }
        ktype = ktype_map.get(timeframe, KLType.K_60M)
        
        ret, data, _ = ctx.request_history_kline(
            pair,
            start=start_date,
            end=end_date,
            ktype=ktype,
            autype=AuType.QFQ
        )
        
        ctx.close()
        
        if ret != 0 or data is None or data.empty:
            logger.warning(f"获取数据失败: {ret}")
            return pd.DataFrame()
        
        # 转换为标准格式
        df = data.set_index('time_key')
        df = df[['open', 'high', 'low', 'close', 'volume']]
        
        # 保存本地
        os.makedirs(os.path.dirname(csv_path), exist_ok=True)
        df.to_csv(csv_path)
        logger.info(f"数据已保存: {csv_path} ({len(df)} bars)")
        
        return df
    
    except ImportError:
        logger.error("futu 包未安装")
        return pd.DataFrame()
    except Exception as e:
        logger.error(f"获取数据失败: {e}")
        return pd.DataFrame()


def cmd_run(args):
    """运行单策略回测"""
    from app.backtest.engine import get_backtest_engine
    
    logger.info(f"=== 回测开始: {args.strategy} ===")
    
    # 获取策略
    strategy = get_strategy(args.strategy)
    logger.info(f"策略: {strategy}")
    
    # 加载数据
    df = load_data(args.pair, args.days, strategy.timeframe)
    if df.empty:
        logger.error("数据为空，退出")
        return
    
    # 运行回测
    engine = get_backtest_engine(
        initial_capital=args.capital,
        fee_pct=args.fee,
        slippage_pct=args.slippage,
        max_open_trades=args.max_trades,
        position_size_pct=args.position_size,
    )
    
    result = engine.run(
        strategy=strategy,
        dataframe=df,
        pair=args.pair,
    )
    
    # 输出结果
    print("\n" + "=" * 60)
    print(result.summary())
    print("=" * 60)
    
    # 保存结果
    output_file = os.path.join(OUTPUT_DIR, f"{args.strategy}_{args.pair}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    result_dict = result.to_dict()
    
    # 添加交易明细
    result_dict['trades'] = [
        {
            'pair': t.pair,
            'enter_date': t.enter_date,
            'exit_date': t.exit_date,
            'enter_price': t.enter_price,
            'exit_price': t.exit_price,
            'profit_pct': t.profit_pct,
            'profit_abs': t.profit_abs,
            'exit_reason': t.exit_reason,
            'duration_bars': t.duration_bars,
        }
        for t in result.trades
    ]
    
    with open(output_file, 'w') as f:
        json.dump(result_dict, f, indent=2, ensure_ascii=False)
    
    logger.info(f"结果已保存: {output_file}")
    
    # 输出权益曲线
    if result.equity_curve:
        equity_file = output_file.replace('.json', '_equity.csv')
        import pandas as pd
        pd.Series(result.equity_curve).to_csv(equity_file)
        logger.info(f"权益曲线已保存: {equity_file}")


def cmd_optimize(args):
    """参数优化"""
    from app.backtest.engine import get_backtest_engine
    
    logger.info(f"=== 参数优化: {args.strategy} ===")
    
    strategy = get_strategy(args.strategy)
    df = load_data(args.pair, args.days, strategy.timeframe)
    
    if df.empty:
        logger.error("数据为空，退出")
        return
    
    engine = get_backtest_engine(
        initial_capital=args.capital,
        fee_pct=args.fee,
        slippage_pct=args.slippage,
    )
    
    param_grid = None
    if args.param_grid:
        param_grid = json.loads(args.param_grid)
    
    result = engine.optimize(
        strategy=strategy,
        dataframe=df,
        pair=args.pair,
        param_grid=param_grid,
        metric=args.metric,
    )
    
    # 输出结果
    print("\n" + "=" * 60)
    print("🏆 最优参数:")
    print(f"   {result.get('best_params')}")
    print()
    
    if result.get('best_result'):
        print(result['best_result'].summary())
    
    print(f"\n⏱️ 优化耗时: {result['optimization_time']:.1f}s")
    print(f"📊 测试组合数: {result['total_combinations']}")
    print("=" * 60)
    
    # 保存结果
    output_file = os.path.join(OUTPUT_DIR, f"optimize_{args.strategy}_{args.pair}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    
    # 只保存汇总 (避免结果文件过大)
    summary = {
        'best_params': result.get('best_params'),
        'optimization_time': result['optimization_time'],
        'total_combinations': result['total_combinations'],
        'all_results': [
            {
                'params': r['params'],
                'score': r['score'],
                'metrics': {
                    'sharpe': r['metrics'].get('sharpe_ratio'),
                    'total_return': r['metrics'].get('total_return_pct'),
                    'win_rate': r['metrics'].get('win_rate'),
                    'trades': r['metrics'].get('total_trades'),
                }
            }
            for r in result.get('all_results', [])
        ],
    }
    
    with open(output_file, 'w') as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    
    logger.info(f"优化结果已保存: {output_file}")


def cmd_walkforward(args):
    """Walk-Forward 验证"""
    from app.backtest.engine import get_backtest_engine
    
    logger.info(f"=== Walk-Forward 验证: {args.strategy} ===")
    
    strategy = get_strategy(args.strategy)
    df = load_data(args.pair, args.days, strategy.timeframe)
    
    if df.empty:
        logger.error("数据为空，退出")
        return
    
    engine = get_backtest_engine(
        initial_capital=args.capital,
        fee_pct=args.fee,
        slippage_pct=args.slippage,
    )
    
    result = engine.walk_forward(
        strategy=strategy,
        dataframe=df,
        train_bars=args.train_bars,
        test_bars=args.test_bars,
        pair=args.pair,
    )
    
    # 输出结果
    print("\n" + "=" * 60)
    print(f"📊 Walk-Forward 验证: {args.strategy}")
    print(f"窗口数: {result.get('num_windows', 0)}")
    print(f"平均 Sharpe: {result.get('avg_sharpe', 0):.3f}")
    print(f"稳定性: {result.get('stability', 0):.3f}")
    print("=" * 60)
    
    for w in result.get('results', []):
        print(
            f"窗口 {w['window']}: "
            f"Sharpe={w['sharpe']:.3f} | "
            f"Return={w['total_return']:.2%} | "
            f"DD={w['max_drawdown']:.2%} | "
            f"Win={w['win_rate']:.0%} | "
            f"Trades={w['trades']}"
        )


def cmd_list(args):
    """列出可用策略"""
    from app.strategy.example_strategies import RSIMeanReversion, MACross, MACDMomentum
    
    strategies = [
        RSIMeanReversion(),
        MACross(),
        MACDMomentum(),
    ]
    
    print("\n" + "=" * 60)
    print("📋 可用策略列表")
    print("=" * 60)
    
    for s in strategies:
        print(f"\n📌 {s.name} (v{s.version})")
        print(f"   描述: {s.description}")
        print(f"   时间框架: {s.timeframe}")
        print(f"   止损: {s.stoploss:.2%}")
        print(f"   追踪止损: {'✅' if s.trailing_stop else '❌'}")
        print(f"   可调参数: {list(s.get_param_grid().keys())}")
    
    print("\n" + "=" * 60)


def main():
    parser = argparse.ArgumentParser(description="FTSeek 回测 CLI")
    parser.add_argument('command', choices=['run', 'optimize', 'walkforward', 'list'],
                       help='命令')
    
    # 通用参数
    parser.add_argument('--strategy', default='rsi_mean_reversion',
                       help='策略名称')
    parser.add_argument('--pair', default='HK.00700',
                       help='交易对')
    parser.add_argument('--days', type=int, default=365,
                       help='回测天数')
    parser.add_argument('--capital', type=float, default=1_000_000,
                       help='初始资金')
    parser.add_argument('--fee', type=float, default=0.001,
                       help='手续费比例')
    parser.add_argument('--slippage', type=float, default=0.001,
                       help='滑点比例')
    parser.add_argument('--max-trades', type=int, default=3,
                       help='最大同时持仓数')
    parser.add_argument('--position-size', type=float, default=0.10,
                       help='每笔仓位比例')
    
    # 优化参数
    parser.add_argument('--param-grid', type=str, default=None,
                       help='参数网格 (JSON)')
    parser.add_argument('--metric', default='sharpe_ratio',
                       help='优化目标指标')
    
    # Walk-Forward 参数
    parser.add_argument('--train-bars', type=int, default=200,
                       help='训练窗口大小')
    parser.add_argument('--test-bars', type=int, default=50,
                       help='测试窗口大小')
    
    args = parser.parse_args()
    
    commands = {
        'run': cmd_run,
        'optimize': cmd_optimize,
        'walkforward': cmd_walkforward,
        'list': cmd_list,
    }
    
    try:
        commands[args.command](args)
    except Exception as e:
        logger.error(f"执行失败: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
