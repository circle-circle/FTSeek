#!/usr/bin/env python3.8
"""
FTSeek 信号查询 CLI 工具

用法:
  python3.8 scripts/query_signals.py [选项]

示例:
  # 查询最近 50 条信号
  python3.8 scripts/query_signals.py
  
  # 查询特定股票
  python3.8 scripts/query_signals.py --stock HK0700 --limit 10
  
  # 查询特定日期范围
  python3.8 scripts/query_signals.py --from 2026-02-01 --to 2026-02-28
  
  # 导出为 JSON
  python3.8 scripts/query_signals.py --stock HK0700 --format json --output signals.json
  
  # 生成日报
  python3.8 scripts/query_signals.py --daily-report --output daily_report.json
"""
import argparse
import sys
import os
from datetime import datetime, timedelta


def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description='FTSeek 信号查询工具 - 从数据库查询交易信号',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
示例:
  %(prog)s --limit 10                    查询最近 10 条信号
  %(prog)s --stock HK0700 --limit 5      查询腾讯的最近 5 条信号
  %(prog)s --status executed --format csv  导出已执行信号为 CSV
  %(prog)s --daily-report --output report.json  生成日报
        '''
    )
    
    # 股票过滤
    parser.add_argument(
        '--stock',
        type=str,
        metavar='CODE',
        help='股票代码 (如 HK0700)'
    )
    
    # 日期范围
    parser.add_argument(
        '--from',
        dest='from_date',
        type=str,
        metavar='YYYY-MM-DD',
        help='开始日期 (YYYY-MM-DD)'
    )
    
    parser.add_argument(
        '--to',
        dest='to_date',
        type=str,
        metavar='YYYY-MM-DD',
        help='结束日期 (YYYY-MM-DD)'
    )
    
    # 信号状态
    parser.add_argument(
        '--status',
        type=str,
        choices=['pending', 'executed', 'ignored'],
        help='信号状态 (pending/executed/ignored)'
    )
    
    # 策略名
    parser.add_argument(
        '--strategy',
        type=str,
        metavar='NAME',
        help='策略名称'
    )
    
    # 限制条数
    parser.add_argument(
        '--limit',
        type=int,
        default=50,
        metavar='N',
        help='限制返回条数 (默认: 50)'
    )
    
    # 输出格式
    parser.add_argument(
        '--format',
        type=str,
        choices=['table', 'json', 'csv'],
        default='table',
        help='输出格式 (默认: table)'
    )
    
    # 输出文件
    parser.add_argument(
        '--output',
        type=str,
        metavar='FILE',
        help='输出文件路径 (默认: stdout)'
    )
    
    # 报告模式
    parser.add_argument(
        '--daily-report',
        action='store_true',
        help='日报模式：查询昨天至今的信号'
    )
    
    parser.add_argument(
        '--weekly-report',
        action='store_true',
        help='周报模式：查询过去 7 天的信号'
    )
    
    return parser.parse_args()


def validate_args(args):
    """验证参数组合"""
    # 日报/周报模式必须指定 output
    if (args.daily_report or args.weekly_report) and not args.output:
        print("错误：日报/周报模式必须指定 --output 参数", file=sys.stderr)
        sys.exit(1)
    
    # 不能同时指定日报和周报
    if args.daily_report and args.weekly_report:
        print("错误：不能同时指定 --daily-report 和 --weekly-report", file=sys.stderr)
        sys.exit(1)
    
    # 验证日期格式
    if args.from_date:
        try:
            datetime.strptime(args.from_date, '%Y-%m-%d')
        except ValueError:
            print(f"错误：无效的日期格式 '{args.from_date}'，应为 YYYY-MM-DD", file=sys.stderr)
            sys.exit(1)
    
    if args.to_date:
        try:
            datetime.strptime(args.to_date, '%Y-%m-%d')
        except ValueError:
            print(f"错误：无效的日期格式 '{args.to_date}'，应为 YYYY-MM-DD", file=sys.stderr)
            sys.exit(1)
    
    # 验证日期范围逻辑
    if args.from_date and args.to_date:
        from_dt = datetime.strptime(args.from_date, '%Y-%m-%d')
        to_dt = datetime.strptime(args.to_date, '%Y-%m-%d')
        if from_dt > to_dt:
            print("错误：开始日期不能晚于结束日期", file=sys.stderr)
            sys.exit(1)


def query_signals(args):
    """
    查询信号数据
    
    根据参数动态构建 SQL WHERE 子句，支持多条件组合查询
    """
    import sqlite3
    
    # 数据库路径
    db_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'data',
        'ftseek.db'
    )
    
    # 检查数据库是否存在
    if not os.path.exists(db_path):
        print(f"错误：数据库文件不存在：{db_path}", file=sys.stderr)
        sys.exit(1)
    
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row  # 返回字典形式的行
        cursor = conn.cursor()
        
        # 构建 WHERE 子句
        conditions = []
        params = []
        
        # 股票代码过滤
        if args.stock:
            conditions.append("code = ?")
            params.append(args.stock)
        
        # 日期范围过滤
        if args.from_date:
            conditions.append("date(created_at) >= date(?)")
            params.append(args.from_date)
        
        if args.to_date:
            # 包含结束日期的整天
            conditions.append("date(created_at) <= date(?)")
            params.append(args.to_date)
        
        # 状态过滤
        if args.status:
            conditions.append("status = ?")
            params.append(args.status)
        
        # 策略名过滤
        if args.strategy:
            conditions.append("strategy = ?")
            params.append(args.strategy)
        
        # 构建完整 SQL
        where_clause = ""
        if conditions:
            where_clause = "WHERE " + " AND ".join(conditions)
        
        sql = f"""
            SELECT id, code, name, action, price, quantity, strategy,
                   technical_score, sentiment_score, confidence, reason,
                   created_at, expire_at, confirmed_at, executed_at, status
            FROM signals
            {where_clause}
            ORDER BY created_at DESC
            LIMIT ?
        """
        params.append(args.limit)
        
        # 执行查询
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        
        # 转换为字典列表
        signals = []
        for row in rows:
            signal = {key: row[key] for key in row.keys()}
            signals.append(signal)
        
        conn.close()
        return signals
        
    except sqlite3.Error as e:
        print(f"数据库错误：{e}", file=sys.stderr)
        sys.exit(1)


def output_table(signals, output_file=None):
    """
    以表格格式输出信号
    
    TODO: Task 3 实现表格输出
    """
    if not signals:
        print("未找到信号数据")
        return
    
    # 简单输出（Task 3 将实现美化表格）
    for signal in signals:
        print(signal)


def output_json(signals, output_file=None):
    """
    以 JSON 格式输出信号
    
    TODO: Task 4 实现 JSON 输出
    """
    import json
    
    if output_file:
        try:
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(signals, f, ensure_ascii=False, indent=2)
            print(f"已写入 {len(signals)} 条信号到 {output_file}")
        except IOError as e:
            print(f"错误：无法写入文件：{e}", file=sys.stderr)
            sys.exit(1)
    else:
        print(json.dumps(signals, ensure_ascii=False, indent=2))


def output_csv(signals, output_file=None):
    """
    以 CSV 格式输出信号
    
    TODO: Task 5 实现 CSV 输出
    """
    import csv
    
    if not signals:
        print("未找到信号数据")
        return
    
    # 定义字段
    fieldnames = ['id', 'code', 'action', 'price', 'confidence', 'status', 'created_at']
    
    if output_file:
        try:
            with open(output_file, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(signals)
            print(f"已写入 {len(signals)} 条信号到 {output_file}")
        except IOError as e:
            print(f"错误：无法写入文件：{e}", file=sys.stderr)
            sys.exit(1)
    else:
        writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(signals)


def generate_report(args, signals):
    """
    生成日报/周报统计信息
    
    TODO: Task 6 完善报告功能
    """
    from datetime import datetime
    
    # 计算日期范围
    if args.daily_report:
        from_date = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
        report_type = 'daily'
    else:  # weekly
        from_date = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
        report_type = 'weekly'
    
    # 统计信息
    stats = {
        'total': len(signals),
        'pending': sum(1 for s in signals if s.get('status') == 'pending'),
        'executed': sum(1 for s in signals if s.get('status') == 'executed'),
        'ignored': sum(1 for s in signals if s.get('status') == 'ignored'),
        'avg_confidence': 0.0
    }
    
    if signals:
        confidences = [s.get('confidence', 0) for s in signals if s.get('confidence')]
        if confidences:
            stats['avg_confidence'] = sum(confidences) / len(confidences)
    
    report = {
        'type': report_type,
        'from_date': from_date,
        'to_date': datetime.now().strftime('%Y-%m-%d'),
        'generated_at': datetime.now().isoformat(),
        'signals': signals,
        'stats': stats
    }
    
    return report


def main():
    """主函数"""
    args = parse_args()
    validate_args(args)
    
    # 处理报告模式
    if args.daily_report or args.weekly_report:
        # 自动设置日期范围
        if args.daily_report:
            args.from_date = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
        else:
            args.from_date = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
        args.to_date = datetime.now().strftime('%Y-%m-%d')
    
    # 查询数据
    signals = query_signals(args)
    
    # 生成报告（如果是报告模式）
    if args.daily_report or args.weekly_report:
        report = generate_report(args, signals)
        output_json([report], args.output)
    else:
        # 根据格式输出
        if args.format == 'table':
            output_table(signals, args.output)
        elif args.format == 'json':
            output_json(signals, args.output)
        elif args.format == 'csv':
            output_csv(signals, args.output)


if __name__ == '__main__':
    main()
