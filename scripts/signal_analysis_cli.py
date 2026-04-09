#!/usr/bin/env python3
"""
信号质量分析 CLI 工具 (P1-1 新增)

用法:
    python scripts/signal_analysis_cli.py [command] [options]

命令:
    summary         - 显示统计摘要
    strategy        - 按策略分析
    regime          - 按市场状态分析
    code            - 按股票代码分析
    report          - 生成完整报告
    export          - 导出数据到 CSV

选项:
    --days N        - 查询天数范围 (默认：90)
    --code CODE     - 股票代码
    --strategy STR  - 策略名称
    --output FILE   - 输出文件路径
"""
import sys
import os
import json
import argparse
from datetime import datetime

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.analytics.signal_analytics import get_analytics


def cmd_summary(args):
    """显示统计摘要"""
    analytics = get_analytics()
    summary = analytics.get_summary(days=args.days)
    
    print("=" * 60)
    print(f"信号质量统计摘要 (最近{args.days}天)")
    print("=" * 60)
    print(f"总交易数：    {summary.get('total_trades', 0)}")
    print(f"盈利交易：    {summary.get('winning_trades', 0)}")
    print(f"亏损交易：    {summary.get('losing_trades', 0)}")
    print(f"胜率：        {summary.get('win_rate', 0):.1%}")
    print(f"平均盈亏：    {summary.get('avg_pnl_pct', 0):.2f}%")
    print(f"总盈亏：      {summary.get('total_pnl_pct', 0):.2f}%")
    print(f"平均 ADX:      {summary.get('avg_adx', 0):.1f}")
    print(f"平均 MTF 对齐：{summary.get('avg_mtf_alignment', 0):.3f}")
    print(f"平均情绪分：  {summary.get('avg_sentiment', 0):.2f}")
    print(f"平均共识分：  {summary.get('avg_consensus', 0):.2f}")
    print("=" * 60)


def cmd_strategy(args):
    """按策略分析"""
    analytics = get_analytics()
    perf = analytics.analyze_strategy_performance(days=args.days)
    
    print("=" * 60)
    print(f"策略表现分析 (最近{args.days}天)")
    print("=" * 60)
    print(f"{'策略':<20} {'交易数':>8} {'胜率':>10} {'平均盈亏':>12} {'总盈亏':>12}")
    print("-" * 60)
    
    for strategy, stats in sorted(perf.items(), key=lambda x: x[1].get('win_rate', 0), reverse=True):
        print(f"{strategy:<20} {stats.get('total_trades', 0):>8} "
              f"{stats.get('win_rate', 0):>10.1%} "
              f"{stats.get('avg_pnl_pct', 0):>12.2f}% "
              f"{stats.get('total_pnl_pct', 0):>12.2f}%")
    
    print("=" * 60)


def cmd_regime(args):
    """按市场状态分析"""
    analytics = get_analytics()
    perf = analytics.analyze_regime_performance(days=args.days)
    
    print("=" * 60)
    print(f"市场状态表现分析 (最近{args.days}天)")
    print("=" * 60)
    print(f"{'市场状态':<15} {'交易数':>8} {'胜率':>10} {'平均盈亏':>12} {'总盈亏':>12}")
    print("-" * 60)
    
    for regime, stats in perf.items():
        print(f"{regime:<15} {stats.get('total_trades', 0):>8} "
              f"{stats.get('win_rate', 0):>10.1%} "
              f"{stats.get('avg_pnl_pct', 0):>12.2f}% "
              f"{stats.get('total_pnl_pct', 0):>12.2f}%")
    
    print("=" * 60)


def cmd_code(args):
    """按股票代码分析"""
    analytics = get_analytics()
    
    if args.code:
        # 查询特定股票
        perf = {args.code: analytics.get_summary(days=args.days, group_by='code')}
        outcomes = analytics.query_by_code(args.code, days=args.days)
        
        print("=" * 60)
        print(f"股票 {args.code} 信号质量分析 (最近{args.days}天)")
        print("=" * 60)
        print(f"交易数：{len(outcomes)}")
        
        if outcomes:
            wins = sum(1 for o in outcomes if o['pnl_pct'] > 0)
            print(f"胜率：{wins/len(outcomes):.1%}")
            print(f"平均盈亏：{sum(o['pnl_pct'] for o in outcomes)/len(outcomes):.2f}%")
    else:
        # 显示所有股票
        summary = analytics.get_summary(days=args.days, group_by='code')
        
        print("=" * 60)
        print(f"股票表现分析 (最近{args.days}天)")
        print("=" * 60)
        print(f"{'代码':<15} {'交易数':>8} {'胜率':>10} {'平均盈亏':>12} {'总盈亏':>12}")
        print("-" * 60)
        
        for row in summary.get('data', []):
            print(f"{row['code']:<15} {row.get('total_trades', 0):>8} "
                  f"{row.get('win_rate', 0):>10.1%} "
                  f"{row.get('avg_pnl_pct', 0):>12.2f}% "
                  f"{row.get('total_pnl_pct', 0):>12.2f}%")
    
    print("=" * 60)


def cmd_report(args):
    """生成完整报告"""
    analytics = get_analytics()
    report = analytics.generate_report(days=args.days)
    
    if args.output:
        with open(args.output, 'w', encoding='utf-8') as f:
            f.write(report)
        print(f"报告已保存到：{args.output}")
    else:
        print(report)


def cmd_export(args):
    """导出数据到 CSV"""
    analytics = get_analytics()
    
    # 获取所有数据
    if args.code:
        outcomes = analytics.query_by_code(args.code, days=args.days)
    elif args.strategy:
        outcomes = analytics.query_by_strategy(args.strategy, days=args.days)
    else:
        # 获取所有记录 (需要遍历所有查询)
        outcomes = []
        for regime in ['trending', 'ranging', 'neutral']:
            outcomes.extend(analytics.query_by_regime(regime, days=args.days))
    
    if not outcomes:
        print("没有数据可导出")
        return
    
    # 写入 CSV
    output_file = args.output or f"signal_outcomes_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    
    with open(output_file, 'w', encoding='utf-8') as f:
        # 表头
        headers = ['code', 'action', 'strategy', 'entry_price', 'exit_price', 
                   'pnl_pct', 'holding_periods', 'market_regime', 'adx_value',
                   'mtf_alignment', 'sentiment_score', 'consensus_score',
                   'quality_score', 'max_drawdown', 'max_profit', 'timestamp']
        f.write(','.join(headers) + '\n')
        
        # 数据
        for o in outcomes:
            row = [str(o.get(h, '')) for h in headers]
            f.write(','.join(row) + '\n')
    
    print(f"已导出 {len(outcomes)} 条记录到：{output_file}")


def cmd_best_conditions(args):
    """分析最佳交易条件"""
    analytics = get_analytics()
    
    if args.strategy:
        result = analytics.analyze_best_conditions(args.strategy, days=args.days)
        print("=" * 60)
        print(f"策略 {args.strategy} 最佳交易条件 (最近{args.days}天)")
        print("=" * 60)
    else:
        result = analytics.analyze_best_conditions(days=args.days)
        print("=" * 60)
        print(f"最佳交易条件分析 (最近{args.days}天)")
        print("=" * 60)
    
    print(f"最佳市场状态：   {result.get('best_regime', 'N/A')} "
          f"(胜率 {result.get('best_regime_winrate', 0):.1%})")
    print(f"最佳 ADX 范围：    {result.get('best_adx_range', 'N/A')} "
          f"(胜率 {result.get('best_adx_winrate', 0):.1%})")
    print(f"最佳 MTF 对齐范围：{result.get('best_mtf_range', 'N/A')} "
          f"(胜率 {result.get('best_mtf_winrate', 0):.1%})")
    print(f"分析样本数：     {result.get('total_analyzed', 0)}")
    print("=" * 60)


def cmd_quality_trend(args):
    """分析质量趋势"""
    analytics = get_analytics()
    
    buckets = analytics.analyze_quality_trend(
        code=args.code,
        days=args.days,
        bucket_days=args.bucket
    )
    
    print("=" * 60)
    print(f"信号质量趋势分析{' - ' + args.code if args.code else ''} (最近{args.days}天)")
    print("=" * 60)
    print(f"{'时间段':<25} {'交易数':>8} {'胜率':>10} {'平均盈亏':>12} {'平均质量':>12}")
    print("-" * 60)
    
    for bucket in buckets:
        print(f"{bucket['period']:<25} {bucket['trades']:>8} "
              f"{bucket['win_rate']:>10.1%} "
              f"{bucket['avg_pnl']:>12.2f}% "
              f"{bucket['avg_quality']:>12.2f}")
    
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(
        description='FTSeek 信号质量分析工具',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    
    subparsers = parser.add_subparsers(dest='command', help='命令')
    
    # summary 命令
    p_summary = subparsers.add_parser('summary', help='显示统计摘要')
    p_summary.add_argument('--days', type=int, default=90, help='查询天数范围')
    p_summary.set_defaults(func=cmd_summary)
    
    # strategy 命令
    p_strategy = subparsers.add_parser('strategy', help='按策略分析')
    p_strategy.add_argument('--days', type=int, default=90, help='查询天数范围')
    p_strategy.set_defaults(func=cmd_strategy)
    
    # regime 命令
    p_regime = subparsers.add_parser('regime', help='按市场状态分析')
    p_regime.add_argument('--days', type=int, default=90, help='查询天数范围')
    p_regime.set_defaults(func=cmd_regime)
    
    # code 命令
    p_code = subparsers.add_parser('code', help='按股票代码分析')
    p_code.add_argument('--days', type=int, default=90, help='查询天数范围')
    p_code.add_argument('--code', type=str, help='股票代码')
    p_code.set_defaults(func=cmd_code)
    
    # report 命令
    p_report = subparsers.add_parser('report', help='生成完整报告')
    p_report.add_argument('--days', type=int, default=90, help='查询天数范围')
    p_report.add_argument('--output', type=str, help='输出文件路径')
    p_report.set_defaults(func=cmd_report)
    
    # export 命令
    p_export = subparsers.add_parser('export', help='导出数据到 CSV')
    p_export.add_argument('--days', type=int, default=90, help='查询天数范围')
    p_export.add_argument('--code', type=str, help='股票代码')
    p_export.add_argument('--strategy', type=str, help='策略名称')
    p_export.add_argument('--output', type=str, help='输出文件路径')
    p_export.set_defaults(func=cmd_export)
    
    # best-conditions 命令
    p_best = subparsers.add_parser('best-conditions', help='分析最佳交易条件')
    p_best.add_argument('--days', type=int, default=90, help='查询天数范围')
    p_best.add_argument('--strategy', type=str, help='策略名称')
    p_best.set_defaults(func=cmd_best_conditions)
    
    # quality-trend 命令
    p_trend = subparsers.add_parser('quality-trend', help='分析质量趋势')
    p_trend.add_argument('--days', type=int, default=90, help='查询天数范围')
    p_trend.add_argument('--code', type=str, help='股票代码')
    p_trend.add_argument('--bucket', type=int, default=7, help='分桶天数')
    p_trend.set_defaults(func=cmd_quality_trend)
    
    args = parser.parse_args()
    
    if args.command is None:
        parser.print_help()
        sys.exit(1)
    
    args.func(args)


if __name__ == '__main__':
    main()
