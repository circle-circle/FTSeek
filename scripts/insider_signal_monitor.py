#!/usr/bin/env python3
"""
Insider Signal Monitor - 监控 INSIDER_CLUSTER_BUY 策略信号质量

功能：
1. 解析 logs/ftseek.log 中的 insider 信号
2. 统计信号生成频率、方向分布
3. 跟踪已生成信号的实际表现（如有）
4. 生成每日/累积报告
"""

import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional
from collections import defaultdict

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent
LOG_FILE = PROJECT_ROOT / "logs" / "ftseek_us.log"
SIGNAL_LOG = PROJECT_ROOT / "logs" / "insider_signals.jsonl"
STATS_DIR = PROJECT_ROOT / "data" / "stats"
REPORTS_DIR = PROJECT_ROOT / "data" / "reports"

# 确保目录存在
STATS_DIR.mkdir(parents=True, exist_ok=True)
REPORTS_DIR.mkdir(parents=True, exist_ok=True)


def parse_insider_signals_from_log(log_file, days_back: int = 1) -> List[Dict]:
    """从主日志解析 insider 信号

    实际日志格式（两行）:
      2026-07-06 18:08:38,800 [...] INFO: [InsiderScan] 🟢 发现 1 个集群买入信号!
      2026-07-06 18:08:38,800 [...] INFO: [InsiderScan] 🟢 内部人集群买入: US.TSM

    Args:
        log_file: Path 对象或已打开的文件对象 (支持 StringIO/gzip 文件)
        days_back: 向前回溯天数
    """
    signals = []
    cutoff = datetime.now() - timedelta(days=days_back)

    # 匹配: [InsiderScan] 🟢 内部人集群买入: US.TSM
    symbol_pattern = re.compile(
        r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+.*?\[InsiderScan\] '
        r'🟢 内部人集群买入: (US\.\w+)'
    )

    # 处理 Path 对象或文件对象
    if hasattr(log_file, 'exists'):
        if not log_file.exists():
            print(f"⚠ Log file not found: {log_file}")
            return signals
        f = open(log_file, 'r', encoding='utf-8', errors='replace')
        close_after = True
    else:
        f = log_file
        close_after = False

    try:
        for line in f:
            match = symbol_pattern.search(line)
            if match:
                timestamp_str, symbol = match.groups()
                timestamp = datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S')

                if timestamp >= cutoff:
                    signals.append({
                        'timestamp': timestamp.isoformat(),
                        'symbol': symbol,
                        'buyers': 0,      # 不在日志行中，留占位
                        'amount': 0,       # 同上
                        'source': 'log'
                    })
    finally:
        if close_after:
            f.close()

    return signals


def parse_insider_signals_from_jsonl(jsonl_file: Path, days_back: int = 7) -> List[Dict]:
    """从 JSONL 文件解析 insider 信号（更详细）"""
    signals = []
    cutoff = datetime.now() - timedelta(days=days_back)
    
    if not jsonl_file.exists():
        return signals
    
    with open(jsonl_file, 'r', encoding='utf-8') as f:
        for line in f:
            try:
                signal = json.loads(line.strip())
                timestamp = datetime.fromisoformat(signal['timestamp'])
                if timestamp >= cutoff:
                    signals.append(signal)
            except (json.JSONDecodeError, KeyError):
                continue
    
    return signals


def calculate_stats(signals: List[Dict]) -> Dict:
    """计算信号统计"""
    if not signals:
        return {
            'total_signals': 0,
            'symbols': [],
            'avg_buyers': 0,
            'avg_amount': 0,
            'total_amount': 0
        }
    
    symbols = set(s['symbol'] for s in signals)
    total_amount = sum(s['amount'] for s in signals)
    avg_buyers = sum(s['buyers'] for s in signals) / len(signals)
    avg_amount = total_amount / len(signals)
    
    # 按 symbol 分组
    by_symbol = defaultdict(list)
    for s in signals:
        by_symbol[s['symbol']].append(s)
    
    return {
        'total_signals': len(signals),
        'symbols': sorted(list(symbols)),
        'symbol_count': len(symbols),
        'avg_buyers': round(avg_buyers, 2),
        'avg_amount': round(avg_amount),
        'total_amount': total_amount,
        'by_symbol': {k: len(v) for k, v in by_symbol.items()}
    }


def generate_daily_report(date_str: Optional[str] = None) -> Dict:
    """生成每日报告"""
    if date_str is None:
        date_str = datetime.now().strftime('%Y-%m-%d')

    # 解析今日信号 — 同时搜索当前日志和最近轮转的日志
    all_log_files = [LOG_FILE]
    # 添加轮转日志 (格式: ftseek_us.log-YYYYMMDD 或 .gz)
    for f in LOG_FILE.parent.glob("ftseek_us.log-*"):
        all_log_files.append(f)

    signals_today = []
    for lf in all_log_files:
        if lf.suffix == '.gz':
            import gzip
            try:
                with gzip.open(lf, 'rt', encoding='utf-8', errors='replace') as f:
                    content = f.read()
                # 写到临时变量供 parse 函数处理
                from io import StringIO
                signals = parse_insider_signals_from_log(StringIO(content), days_back=1) if isinstance(content, str) else []
            except Exception:
                continue
        else:
            signals = parse_insider_signals_from_log(lf, days_back=1)
        signals_today.extend(signals)
    signals_today += parse_insider_signals_from_jsonl(SIGNAL_LOG, days_back=1)
    
    # 去重（按 timestamp + symbol）
    seen = set()
    unique_signals = []
    for s in signals_today:
        key = (s['timestamp'], s['symbol'])
        if key not in seen:
            seen.add(key)
            unique_signals.append(s)
    
    stats = calculate_stats(unique_signals)
    
    report = {
        'date': date_str,
        'generated_at': datetime.now().isoformat(),
        'stats': stats,
        'signals': unique_signals
    }
    
    # 保存报告
    report_file = REPORTS_DIR / f"insider_daily_{date_str}.json"
    with open(report_file, 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    
    return report


def generate_cumulative_stats() -> Dict:
    """生成累积统计"""
    # 解析最近 14 天信号 — 搜索当前日志 + 所有轮转日志
    all_log_files = [LOG_FILE]
    for f in LOG_FILE.parent.glob("ftseek_us.log-*"):
        all_log_files.append(f)

    signals_14d = []
    for lf in all_log_files:
        if lf.suffix == '.gz':
            import gzip
            try:
                with gzip.open(lf, 'rt', encoding='utf-8', errors='replace') as gz_f:
                    signals_14d.extend(parse_insider_signals_from_log(gz_f, days_back=14))
            except Exception:
                continue
        else:
            signals_14d.extend(parse_insider_signals_from_log(lf, days_back=14))
    signals_14d += parse_insider_signals_from_jsonl(SIGNAL_LOG, days_back=14)
    
    # 去重
    seen = set()
    unique_signals = []
    for s in signals_14d:
        key = (s['timestamp'], s['symbol'])
        if key not in seen:
            seen.add(key)
            unique_signals.append(s)
    
    stats_14d = calculate_stats(unique_signals)
    
    # 按周分组
    now = datetime.now()
    week1 = [s for s in unique_signals if 
             datetime.fromisoformat(s['timestamp']) >= now - timedelta(days=7)]
    week2 = [s for s in unique_signals if 
             now - timedelta(days=14) <= datetime.fromisoformat(s['timestamp']) < now - timedelta(days=7)]
    
    stats = {
        'generated_at': datetime.now().isoformat(),
        'period': '14_days',
        'stats_14d': stats_14d,
        'stats_week1': calculate_stats(week1),
        'stats_week2': calculate_stats(week2),
        'trend': {
            'week1_count': len(week1),
            'week2_count': len(week2),
            'change': len(week1) - len(week2)
        }
    }
    
    # 保存累积统计
    stats_file = STATS_DIR / "insider_cumulative.json"
    with open(stats_file, 'w', encoding='utf-8') as f:
        json.dump(stats, f, indent=2, ensure_ascii=False)
    
    return stats


def print_summary(report: Dict):
    """打印报告摘要"""
    stats = report['stats']
    print(f"\n{'='*60}")
    print(f"Insider Signal Monitor - {report['date']}")
    print(f"{'='*60}")
    
    if stats['total_signals'] == 0:
        print("\n⚠ No signals generated today")
        print("建议：检查 scheduler 是否正常运行，或等待下一个交易日")
    else:
        print(f"\n✓ Total signals: {stats['total_signals']}")
        print(f"✓ Unique symbols: {stats['symbol_count']} ({', '.join(stats['symbols'][:5])}{'...' if len(stats['symbols']) > 5 else ''})")
        print(f"✓ Avg buyers: {stats['avg_buyers']}")
        print(f"✓ Avg amount: ${stats['avg_amount']:,}")
        print(f"✓ Total amount: ${stats['total_amount']:,}")
        
        print(f"\nBy symbol:")
        for symbol, count in sorted(stats['by_symbol'].items(), key=lambda x: -x[1])[:10]:
            print(f"  {symbol}: {count} signals")
    
    print(f"\n{'='*60}\n")


def main():
    """主函数"""
    import argparse
    parser = argparse.ArgumentParser(description='Insider Signal Monitor')
    parser.add_argument('--report', choices=['daily', 'cumulative', 'both'], 
                       default='daily', help='Report type')
    parser.add_argument('--date', type=str, help='Report date (YYYY-MM-DD)')
    
    args = parser.parse_args()
    
    if args.report in ('daily', 'both'):
        report = generate_daily_report(args.date)
        print_summary(report)
    
    if args.report in ('cumulative', 'both'):
        stats = generate_cumulative_stats()
        print(f"\nCumulative Stats (14 days):")
        print(f"  Total signals: {stats['stats_14d']['total_signals']}")
        print(f"  Week 1: {stats['trend']['week1_count']} signals")
        print(f"  Week 2: {stats['trend']['week2_count']} signals")
        print(f"  Trend: {'+' if stats['trend']['change'] > 0 else ''}{stats['trend']['change']}")


if __name__ == '__main__':
    main()
