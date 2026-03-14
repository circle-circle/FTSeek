#!/usr/bin/env python3
"""
回测对比报告 CLI 工具

使用示例:
    python3 scripts/backtest_compare_report.py --period 30d
    python3 scripts/backtest_compare_report.py --period 7d --code HK0700
    python3 scripts/backtest_compare_report.py --period 90d --strategy momentum
"""
import argparse
import sys
import os
from datetime import datetime

# 添加项目根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.performance.backtest_compare import BacktestComparer


def format_percentage(value: float, is_rate: bool = False) -> str:
    """格式化百分比"""
    if is_rate:
        return f"{value:.1%}"
    return f"{value:.2f}%"


def format_number(value: float) -> str:
    """格式化数字"""
    if value >= 1000000:
        return f"{value/1000000:.2f}M"
    elif value >= 1000:
        return f"{value/1000:.2f}K"
    return f"{value:.2f}"


def print_report(report: dict, verbose: bool = False):
    """打印对比报告"""
    print("\n" + "=" * 60)
    print("📊 FTSeek 回测对比报告")
    print("=" * 60)
    print(f"时间段：{report['period']}")
    print(f"股票代码：{report['code']}")
    print(f"策略：{report['strategy']}")
    print(f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # 收益率对比
    print("\n📈 收益率对比")
    print("-" * 60)
    returns = report['returns']
    print(f"  实盘收益率：  {format_percentage(returns['live_return'])}")
    print(f"  回测收益率：  {format_percentage(returns['backtest_return'])}")
    diff_symbol = "↑" if returns['difference'] > 0 else "↓" if returns['difference'] < 0 else "="
    diff_color = "+" if returns['difference'] > 0 else ""
    print(f"  差异：        {diff_color}{diff_symbol}{format_percentage(returns['difference'])}")

    # 胜率对比
    print("\n🎯 胜率对比")
    print("-" * 60)
    win_rates = report['win_rates']
    print(f"  实盘胜率：    {format_percentage(win_rates['live_win_rate'], is_rate=True)}")
    print(f"  回测胜率：    {format_percentage(win_rates['backtest_win_rate'], is_rate=True)}")
    diff_symbol = "↑" if win_rates['difference'] > 0 else "↓" if win_rates['difference'] < 0 else "="
    diff_color = "+" if win_rates['difference'] > 0 else ""
    print(f"  差异：        {diff_color}{diff_symbol}{format_percentage(win_rates['difference'], is_rate=True)}")

    # 交易次数对比
    print("\n💼 交易次数对比")
    print("-" * 60)
    trades = report['trades']
    print(f"  实盘交易：    {trades['live_trades']} 笔")
    print(f"  回测交易：    {trades['backtest_trades']} 笔")
    diff_symbol = "↑" if trades['difference'] > 0 else "↓" if trades['difference'] < 0 else "="
    print(f"  差异：        {diff_symbol}{abs(trades['difference'])} 笔")

    # 平均收益对比
    print("\n💰 平均收益对比")
    print("-" * 60)
    avg_profits = report['avg_profits']
    print(f"  实盘平均收益：{format_number(avg_profits['live_avg_profit'])}")
    print(f"  回测平均收益：{format_number(avg_profits['backtest_avg_profit'])}")
    diff_symbol = "↑" if avg_profits['difference'] > 0 else "↓" if avg_profits['difference'] < 0 else "="
    diff_color = "+" if avg_profits['difference'] > 0 else ""
    print(f"  差异：        {diff_color}{diff_symbol}{format_number(avg_profits['difference'])}")

    # 总结
    print("\n📝 总结")
    print("-" * 60)
    print(f"  {report['summary']}")

    # 详细数据 (verbose 模式)
    if verbose:
        print("\n📋 详细数据")
        print("-" * 60)
        print("\n收益率详情:")
        print(f"  实盘：{returns['live_return']:.4f}%")
        print(f"  回测：{returns['backtest_return']:.4f}%")
        print(f"  差异：{returns['difference']:.4f}%")

        print("\n胜率详情:")
        print(f"  实盘：{win_rates['live_win_rate']:.4f}")
        print(f"  回测：{win_rates['backtest_win_rate']:.4f}")
        print(f"  差异：{win_rates['difference']:.4f}")

    print("\n" + "=" * 60)


def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description='FTSeek 回测对比报告工具',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s --period 30d
  %(prog)s --period 7d --code HK0700
  %(prog)s --period 90d --strategy momentum
  %(prog)s --period 30d --verbose
        """
    )

    parser.add_argument(
        '--period', '-p',
        type=str,
        default='30d',
        help='时间段 (如：7d, 30d, 90d), 默认：30d'
    )

    parser.add_argument(
        '--code', '-c',
        type=str,
        default=None,
        help='股票代码 (如：HK0700), 默认：全部'
    )

    parser.add_argument(
        '--strategy', '-s',
        type=str,
        default=None,
        help='策略名称 (如：momentum), 默认：全部'
    )

    parser.add_argument(
        '--db',
        type=str,
        default=None,
        help='数据库路径，默认：/opt/ftseek/FTSeek/data/ftseek.db'
    )

    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='显示详细数据'
    )

    parser.add_argument(
        '--json',
        action='store_true',
        help='以 JSON 格式输出'
    )
    
    parser.add_argument(
        '--push',
        action='store_true',
        help='推送到钉钉'
    )

    args = parser.parse_args()

    try:
        # 创建对比器
        comparer = BacktestComparer(db_path=args.db)

        # 生成报告
        report = comparer.generate_report(
            period=args.period,
            code=args.code,
            strategy=args.strategy
        )

        # 输出报告
        if args.json:
            import json
            print(json.dumps(report, indent=2, ensure_ascii=False))
        else:
            print_report(report, verbose=args.verbose)
        
        # 推送到钉钉
        if args.push:
            import os
            import hashlib
            import hmac
            import base64
            import urllib.parse
            import requests
            from datetime import datetime
            
            # 从环境变量获取配置
            webhook_url = os.environ.get('DINGTALK_WEBHOOK_URL')
            secret = os.environ.get('DINGTALK_SECRET')
            
            if not webhook_url:
                print("⚠️  DINGTALK_WEBHOOK_URL 未配置，跳过推送", file=sys.stderr)
            else:
                try:
                    # 生成签名
                    timestamp = str(round(datetime.now().timestamp() * 1000))
                    sign_str = f"{timestamp}\n{secret}"
                    sign = base64.b64encode(
                        hmac.new(
                            secret.encode('utf-8'),
                            sign_str.encode('utf-8'),
                            digestmod=hashlib.sha256
                        ).digest()
                    ).decode()
                    sign = urllib.parse.quote(sign, safe='')
                    
                    # 构建消息
                    report_text = f"""📊 FTSeek 回测对比报告

时间段：{report['period']}
股票代码：{report['code']}
策略：{report['strategy']}

📈 收益率对比
- 实盘：{report['returns']['live_return']:.2%}
- 回测：{report['returns']['backtest_return']:.2%}
- 差异：{report['returns']['difference']:+.2%}

🎯 胜率对比
- 实盘：{report['win_rates']['live_win_rate']:.1%}
- 回测：{report['win_rates']['backtest_win_rate']:.1%}
- 差异：{report['win_rates']['difference']:+.1%}

💼 交易次数
- 实盘：{report['trades']['live_trades']} 笔
- 回测：{report['trades']['backtest_trades']} 笔

{report['summary']}"""
                    
                    # 发送消息
                    full_url = f"{webhook_url}&timestamp={timestamp}&sign={sign}"
                    
                    headers = {'Content-Type': 'application/json'}
                    data = {
                        "msgtype": "markdown",
                        "markdown": {
                            "title": "FTSeek 回测对比报告",
                            "text": report_text
                        }
                    }
                    
                    response = requests.post(full_url, json=data, headers=headers, timeout=10)
                    
                    if response.status_code == 200:
                        result = response.json()
                        if result.get('errcode') == 0:
                            print("✅ 报告已推送到钉钉")
                        else:
                            print(f"❌ 钉钉推送失败：{result}", file=sys.stderr)
                    else:
                        print(f"❌ 钉钉推送失败：HTTP {response.status_code}", file=sys.stderr)
                        
                except Exception as e:
                    print(f"❌ 钉钉推送异常：{e}", file=sys.stderr)

    except Exception as e:
        print(f"❌ 错误：{e}", file=sys.stderr)
        if args.verbose:
            import traceback
            traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()
