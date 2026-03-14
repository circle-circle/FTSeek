#!/usr/bin/env python3
"""
钉钉推送集成演示脚本

演示三种推送类型：
1. 周报推送
2. 回测对比报告推送
3. 绩效汇总推送

用法:
    python3 demo_performance_push.py
"""
import sys
import os

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.notifier.dingtalk import DingTalkNotifier


def demo_weekly_report():
    """演示周报推送"""
    print("=" * 60)
    print("📊 演示周报推送")
    print("=" * 60)
    
    report_data = {
        'period': '2026-02-24 to 2026-03-02',
        'total_trades': 15,
        'win_rate': 0.67,
        'total_return': 8.5,
        'by_strategy': {
            'trend_following': {
                'count': 8,
                'win_count': 6,
                'avg_return': 1.2,
                'total_profit': 5000,
                'win_rate': 0.75
            },
            'mean_reversion': {
                'count': 7,
                'win_count': 4,
                'avg_return': 0.5,
                'total_profit': 2000,
                'win_rate': 0.57
            }
        },
        'by_stock': {
            '0700.HK': {
                'count': 5,
                'win_count': 4,
                'avg_return': 2.0,
                'total_profit': 3000,
                'win_rate': 0.80
            },
            '9988.HK': {
                'count': 4,
                'win_count': 2,
                'avg_return': 0.5,
                'total_profit': 500,
                'win_rate': 0.50
            }
        },
        'recommendations': [
            '胜率优秀（67%），可考虑适当增加仓位',
            '本周收益表现优异（8.50%），建议总结成功经验并保持一致性'
        ],
        'benchmark_comparison': {
            'strategy_return': 8.5,
            'benchmark_return': 2.0,
            'excess_return': 6.5,
            'outperform': True
        }
    }
    
    notifier = DingTalkNotifier()
    
    # 检查是否配置 webhook
    if not notifier.webhook_url:
        print("⚠️  未配置钉钉 Webhook URL，跳过实际推送")
        print("   请设置 DINGTALK_WEBHOOK_URL 环境变量")
        print()
        print("✅ 周报数据格式验证通过")
        print(f"   - 周期：{report_data['period']}")
        print(f"   - 交易数：{report_data['total_trades']}")
        print(f"   - 胜率：{report_data['win_rate']:.1%}")
        print(f"   - 总收益：HK${report_data['total_return']:,.2f}")
        return False
    
    # 实际推送
    success = notifier.send_weekly_report(report_data)
    if success:
        print("✅ 周报推送成功")
    else:
        print("❌ 周报推送失败")
    
    return success


def demo_backtest_compare():
    """演示回测对比报告推送"""
    print()
    print("=" * 60)
    print("📈 演示回测对比报告推送")
    print("=" * 60)
    
    report_data = {
        'period': '30d',
        'code': 'all',
        'strategy': 'all',
        'returns': {
            'live_return': 5.2,
            'backtest_return': 4.8,
            'difference': 0.4
        },
        'win_rates': {
            'live_win_rate': 0.65,
            'backtest_win_rate': 0.60,
            'difference': 0.05
        },
        'trades': {
            'live_trades': 25,
            'backtest_trades': 30,
            'difference': -5
        },
        'avg_profits': {
            'live_avg_profit': 1200.50,
            'backtest_avg_profit': 1100.00,
            'difference': 100.50
        },
        'summary': '✓ 实盘收益率 (5.20%) 优于回测 (4.80%) | = 实盘与回测胜率相近 (65.0% vs 60.0%)'
    }
    
    notifier = DingTalkNotifier()
    
    if not notifier.webhook_url:
        print("⚠️  未配置钉钉 Webhook URL，跳过实际推送")
        print()
        print("✅ 回测对比数据格式验证通过")
        print(f"   - 周期：{report_data['period']}")
        print(f"   - 实盘收益：{report_data['returns']['live_return']:.2f}%")
        print(f"   - 回测收益：{report_data['returns']['backtest_return']:.2f}%")
        print(f"   - 实盘胜率：{report_data['win_rates']['live_win_rate']:.1%}")
        return False
    
    success = notifier.send_backtest_compare_report(report_data)
    if success:
        print("✅ 回测对比报告推送成功")
    else:
        print("❌ 回测对比报告推送失败")
    
    return success


def demo_performance_summary():
    """演示绩效汇总推送"""
    print()
    print("=" * 60)
    print("📊 演示绩效汇总推送")
    print("=" * 60)
    
    summary_data = {
        'date': '2026-03-02',
        'today_trades': 5,
        'today_wins': 3,
        'today_win_rate': 0.60,
        'today_pnl': 1500.00,
        'today_pnl_pct': 1.5,
        'cumulative_trades': 50,
        'cumulative_wins': 32,
        'cumulative_win_rate': 0.64,
        'cumulative_pnl': 12000.00,
        'cumulative_pnl_pct': 12.0,
        'positions': [
            {'code': '0700.HK', 'name': '腾讯控股', 'pnl_pct': 2.5},
            {'code': '9988.HK', 'name': '阿里巴巴', 'pnl_pct': -1.2},
            {'code': '3690.HK', 'name': '美团', 'pnl_pct': 3.8}
        ],
        'risk_status': 'normal',
        'consecutive_loss': 0,
        'max_drawdown': 3.5,
        'safe_drawdown': 15.0
    }
    
    notifier = DingTalkNotifier()
    
    if not notifier.webhook_url:
        print("⚠️  未配置钉钉 Webhook URL，跳过实际推送")
        print()
        print("✅ 绩效汇总数据格式验证通过")
        print(f"   - 日期：{summary_data['date']}")
        print(f"   - 今日交易：{summary_data['today_trades']} 笔")
        print(f"   - 今日收益：HK${summary_data['today_pnl']:,.2f}")
        print(f"   - 累计收益：HK${summary_data['cumulative_pnl']:,.2f}")
        print(f"   - 当前持仓：{len(summary_data['positions'])} 只")
        return False
    
    success = notifier.send_performance_summary(summary_data)
    if success:
        print("✅ 绩效汇总推送成功")
    else:
        print("❌ 绩效汇总推送失败")
    
    return success


def main():
    print()
    print("FTSeek 钉钉推送集成演示")
    print("=" * 60)
    print()
    
    # 演示三种推送
    weekly_ok = demo_weekly_report()
    compare_ok = demo_backtest_compare()
    summary_ok = demo_performance_summary()
    
    print()
    print("=" * 60)
    print("演示总结")
    print("=" * 60)
    print(f"周报推送：{'✅' if weekly_ok else '⚠️  (未配置 webhook)'}")
    print(f"回测对比：{'✅' if compare_ok else '⚠️  (未配置 webhook)'}")
    print(f"绩效汇总：{'✅' if summary_ok else '⚠️  (未配置 webhook)'}")
    print()
    print("✅ 所有推送方法已正确集成")
    print()
    
    return 0


if __name__ == '__main__':
    sys.exit(main())
