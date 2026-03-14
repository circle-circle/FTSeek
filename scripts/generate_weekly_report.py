#!/usr/bin/env python3
"""
周报复盘 CLI 工具

生成并输出周度绩效报告

用法:
    python3 generate_weekly_report.py --week this
    python3 generate_weekly_report.py --week last
    python3 generate_weekly_report.py --start 2026-02-01 --end 2026-02-07
    python3 generate_weekly_report.py --push  # 推送到钉钉
"""
import argparse
import sys
import os
from datetime import datetime

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.performance.weekly_review import WeeklyReviewer, get_weekly_reviewer


def main():
    parser = argparse.ArgumentParser(
        description='FTSeek 周报复盘工具',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s --week this              # 生成本周报告
  %(prog)s --week last              # 生成上周报告
  %(prog)s --start 2026-02-01 --end 2026-02-07  # 生成指定周期报告
  %(prog)s --week this --push       # 生成并推送到钉钉
        """
    )

    # 时间范围选项
    time_group = parser.add_mutually_exclusive_group()
    time_group.add_argument(
        '--week',
        choices=['this', 'last'],
        help='生成本周或上周的报告'
    )
    time_group.add_argument(
        '--start',
        help='开始日期 (YYYY-MM-DD)',
        metavar='DATE'
    )
    time_group.add_argument(
        '--end',
        help='结束日期 (YYYY-MM-DD)',
        metavar='DATE'
    )

    # 输出选项
    parser.add_argument(
        '--output', '-o',
        help='输出文件路径（默认输出到控制台）',
        metavar='FILE'
    )
    parser.add_argument(
        '--format',
        choices=['text', 'json'],
        default='text',
        help='输出格式（默认：text）'
    )

    # 推送选项
    parser.add_argument(
        '--push',
        action='store_true',
        help='推送到钉钉'
    )
    parser.add_argument(
        '--webhook',
        help='钉钉 webhook URL（可选，默认使用环境变量 DINGTALK_WEBHOOK）',
        metavar='URL'
    )

    # 数据库选项
    parser.add_argument(
        '--db',
        help='数据库路径（默认：/opt/ftseek/FTSeek/data/ftseek.db）',
        metavar='PATH'
    )

    # 详细模式
    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='显示详细信息'
    )

    args = parser.parse_args()

    # 初始化复盘器
    db_path = args.db or '/opt/ftseek/FTSeek/data/ftseek.db'
    reviewer = get_weekly_reviewer(db_path=db_path)

    # 确定时间范围
    if args.week:
        week_start, week_end = reviewer.get_week_range(args.week)
    elif args.start and args.end:
        week_start = args.start
        week_end = args.end
    elif args.start:
        # 只有开始日期，默认 7 天
        week_start = args.start
        start_date = datetime.strptime(args.start, '%Y-%m-%d')
        end_date = start_date + timedelta(days=6)
        week_end = end_date.strftime('%Y-%m-%d')
    else:
        # 默认本周
        week_start, week_end = reviewer.get_week_range('this')

    if args.verbose:
        print(f"生成报告周期：{week_start} 至 {week_end}")
        print(f"数据库：{db_path}")
        print()

    # 生成报告
    try:
        report = reviewer.generate_report(week_start, week_end)
    except Exception as e:
        print(f"❌ 生成报告失败：{e}", file=sys.stderr)
        sys.exit(1)

    # 输出报告
    if args.format == 'json':
        import json
        output = json.dumps(report, indent=2, ensure_ascii=False)
    else:
        output = reviewer.format_report_text(report)

    # 写入文件或控制台
    if args.output:
        try:
            with open(args.output, 'w', encoding='utf-8') as f:
                f.write(output)
            print(f"✅ 报告已保存到：{args.output}")
        except IOError as e:
            print(f"❌ 保存文件失败：{e}", file=sys.stderr)
            sys.exit(1)
    else:
        print(output)

    # 推送到钉钉
    if args.push:
        webhook_url = args.webhook or os.environ.get('DINGTALK_WEBHOOK')
        
        if not webhook_url:
            print("⚠️  未提供钉钉 webhook URL，跳过推送", file=sys.stderr)
            print("   请设置 --webhook 参数或 DINGTALK_WEBHOOK 环境变量")
        else:
            try:
                success = reviewer.push_to_dingtalk(report, webhook_url)
                if success:
                    print("✅ 报告已推送到钉钉")
                else:
                    print("❌ 钉钉推送失败", file=sys.stderr)
                    sys.exit(1)
            except Exception as e:
                print(f"❌ 钉钉推送失败：{e}", file=sys.stderr)
                sys.exit(1)

    return 0


if __name__ == '__main__':
    # 需要导入 timedelta
    from datetime import timedelta
    sys.exit(main())
