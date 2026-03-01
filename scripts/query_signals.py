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
import logging
from datetime import datetime, timedelta

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


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


def _validate_date_format(date_str: str, param_name: str) -> datetime:
    """验证日期格式并返回 datetime 对象"""
    try:
        logger.debug(f"验证日期参数 {param_name}: {date_str}")
        return datetime.strptime(date_str, '%Y-%m-%d')
    except ValueError:
        logger.error(f"日期格式错误：'{date_str}'，应为 YYYY-MM-DD 格式")
        print(f"错误：无效的日期格式 '{date_str}'，应为 YYYY-MM-DD 格式（例如：2026-03-01）", file=sys.stderr)
        sys.exit(1)


def validate_args(args):
    """验证参数组合"""
    logger.debug("开始验证命令行参数")
    
    # 日报/周报模式必须指定 output
    if (args.daily_report or args.weekly_report) and not args.output:
        logger.error("日报/周报模式未指定 --output 参数")
        print("错误：日报/周报模式必须指定 --output 参数", file=sys.stderr)
        sys.exit(1)
    
    # 不能同时指定日报和周报
    if args.daily_report and args.weekly_report:
        logger.error("同时指定了 --daily-report 和 --weekly-report")
        print("错误：不能同时指定 --daily-report 和 --weekly-report", file=sys.stderr)
        sys.exit(1)
    
    # 验证日期格式
    from_dt = None
    to_dt = None
    
    if args.from_date:
        from_dt = _validate_date_format(args.from_date, 'from_date')
    
    if args.to_date:
        to_dt = _validate_date_format(args.to_date, 'to_date')
    
    # 验证日期范围逻辑
    if from_dt and to_dt and from_dt > to_dt:
        logger.error(f"日期范围错误：开始日期 ({args.from_date}) 晚于结束日期 ({args.to_date})")
        print("错误：开始日期不能晚于结束日期", file=sys.stderr)
        sys.exit(1)
    
    logger.debug("参数验证通过")


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
    
    logger.info(f"数据库路径：{db_path}")
    
    # 检查数据库是否存在
    if not os.path.exists(db_path):
        logger.error(f"数据库文件不存在：{db_path}")
        print(f"错误：数据库文件不存在：{db_path}", file=sys.stderr)
        print("提示：请确保数据库已初始化，或检查数据目录是否正确", file=sys.stderr)
        sys.exit(1)
    
    try:
        logger.debug(f"连接数据库：{db_path}")
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            # 构建 WHERE 子句
            conditions = []
            params = []
            
            # 股票代码过滤
            if args.stock:
                conditions.append("code = ?")
                params.append(args.stock)
                logger.debug(f"添加股票过滤条件：code = {args.stock}")
            
            # 日期范围过滤
            if args.from_date:
                conditions.append("date(created_at) >= date(?)")
                params.append(args.from_date)
                logger.debug(f"添加开始日期过滤：>= {args.from_date}")
            
            if args.to_date:
                # 包含结束日期的整天
                conditions.append("date(created_at) <= date(?)")
                params.append(args.to_date)
                logger.debug(f"添加结束日期过滤：<= {args.to_date}")
            
            # 状态过滤
            if args.status:
                conditions.append("status = ?")
                params.append(args.status)
                logger.debug(f"添加状态过滤：status = {args.status}")
            
            # 策略名过滤
            if args.strategy:
                conditions.append("strategy = ?")
                params.append(args.strategy)
                logger.debug(f"添加策略过滤：strategy = {args.strategy}")
            
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
            logger.debug(f"执行 SQL 查询，限制条数：{args.limit}")
            
            # 执行查询
            cursor.execute(sql, params)
            rows = cursor.fetchall()
            
            # 转换为字典列表
            signals = []
            for row in rows:
                signal = {key: row[key] for key in row.keys()}
                signals.append(signal)
            
            logger.info(f"查询成功，返回 {len(signals)} 条信号")
            return signals
        
    except sqlite3.DatabaseError as e:
        logger.error(f"数据库错误：{e}")
        print(f"错误：数据库操作失败 - {e}", file=sys.stderr)
        print("提示：数据库文件可能已损坏或权限不足", file=sys.stderr)
        sys.exit(1)
    except sqlite3.Error as e:
        logger.error(f"SQLite 错误：{e}")
        print(f"错误：数据库连接失败 - {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        logger.error(f"未知错误：{e}", exc_info=True)
        print(f"错误：查询过程中发生未知错误 - {e}", file=sys.stderr)
        sys.exit(1)


def output_table(signals, output_file=None):
    """
    以表格格式输出信号
    
    尝试使用 tabulate 库美化输出，如果不可用则降级为简单表格
    显示关键字段：id, code, action, price, confidence, status, created_at
    """
    # 定义要显示的关键字段
    display_fields = ['id', 'code', 'action', 'price', 'confidence', 'status', 'created_at']
    
    # 处理空数据
    if not signals:
        logger.info("查询结果为空，无数据显示")
        print("无数据")
        return
    
    logger.debug(f"以表格格式输出 {len(signals)} 条信号")
    
    # 尝试导入 tabulate
    try:
        from tabulate import tabulate
        use_tabulate = True
        logger.debug("使用 tabulate 库格式化输出")
    except ImportError:
        use_tabulate = False
        logger.warning("tabulate 库未安装，使用简单表格格式")
    
    # 准备表格数据
    # 表头（使用中文）
    headers_map = {
        'id': 'ID',
        'code': '股票代码',
        'action': '操作',
        'price': '价格',
        'confidence': '置信度',
        'status': '状态',
        'created_at': '创建时间'
    }
    
    if use_tabulate:
        # 使用 tabulate 美化输出
        headers = [headers_map[field] for field in display_fields]
        table_data = []
        
        for signal in signals:
            row = []
            for field in display_fields:
                value = signal.get(field, '')
                # 格式化价格（保留 2 位小数）
                if field == 'price' and value is not None:
                    value = f"{value:.2f}"
                # 格式化置信度（保留 2 位小数）
                elif field == 'confidence' and value is not None:
                    value = f"{value:.2f}"
                # 处理 None 值
                elif value is None:
                    value = ''
                row.append(str(value))
            table_data.append(row)
        
        # 生成表格（使用 grid 格式，支持中文）
        table_str = tabulate(table_data, headers=headers, tablefmt='grid')
    else:
        # 降级为简单表格
        # 计算每列的最大宽度
        col_widths = {}
        for field in display_fields:
            # 表头宽度
            max_width = len(headers_map[field])
            # 数据宽度
            for signal in signals:
                value = signal.get(field, '')
                if field == 'price' and value is not None:
                    value = f"{value:.2f}"
                elif field == 'confidence' and value is not None:
                    value = f"{value:.2f}"
                elif value is None:
                    value = ''
                max_width = max(max_width, len(str(value)))
            col_widths[field] = max_width
        
        # 构建表格
        lines = []
        
        # 表头
        header_parts = []
        for field in display_fields:
            header_parts.append(headers_map[field].ljust(col_widths[field]))
        lines.append('  '.join(header_parts))
        
        # 分隔线
        separator_parts = []
        for field in display_fields:
            separator_parts.append('-' * col_widths[field])
        lines.append('  '.join(separator_parts))
        
        # 数据行
        for signal in signals:
            row_parts = []
            for field in display_fields:
                value = signal.get(field, '')
                if field == 'price' and value is not None:
                    value = f"{value:.2f}"
                elif field == 'confidence' and value is not None:
                    value = f"{value:.2f}"
                elif value is None:
                    value = ''
                row_parts.append(str(value).ljust(col_widths[field]))
            lines.append('  '.join(row_parts))
        
        table_str = '\n'.join(lines)
    
    # 输出
    if output_file:
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(table_str)
            f.write('\n')
        print(f"已写入 {len(signals)} 条信号到 {output_file}")
    else:
        print(table_str)


def output_json(signals, output_file=None):
    """
    以 JSON 格式输出信号
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
    """
    import csv
    
    if not signals:
        print("未找到信号数据")
        return
    
    # 定义字段 - 包含所有可能的字段
    fieldnames = ['id', 'code', 'name', 'action', 'price', 'quantity', 'strategy',
                  'technical_score', 'sentiment_score', 'confidence', 'reason',
                  'created_at', 'expire_at', 'confirmed_at', 'executed_at', 'status']
    
    if output_file:
        try:
            with open(output_file, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
                writer.writeheader()
                writer.writerows(signals)
            print(f"已写入 {len(signals)} 条信号到 {output_file}")
        except IOError as e:
            print(f"错误：无法写入文件：{e}", file=sys.stderr)
            sys.exit(1)
    else:
        writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(signals)


def generate_report(args, signals):
    """
    生成日报/周报统计信息
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
