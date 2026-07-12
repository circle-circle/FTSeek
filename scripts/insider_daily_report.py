#!/usr/bin/env python3
"""Insider Signal Monitor - 每日信号质量报告

修复历史问题:
1. 日志文件名 ftseek.log → ftseek_us.log (实际文件名)
2. 同时搜索当前日志 + 轮转日志 (ftseek_us.log-YYYYMMDD)
3. 匹配实际日志格式: "[InsiderScan] 🟢 内部人集群买入: US.TSM"
"""
import sys
sys.path.insert(0, '/opt/ftseek/FTSeek-US')
from pathlib import Path
import gzip
import json
import re
from datetime import datetime, timedelta
from collections import defaultdict

LOG_DIR = Path('/opt/ftseek/FTSeek-US/logs')
CURRENT_LOG = LOG_DIR / "ftseek_us.log"
SIGNAL_LOG = LOG_DIR / "insider_signals.jsonl"

# 匹配: [InsiderScan] 🟢 内部人集群买入: US.TSM
SIGNAL_PATTERN = re.compile(
    r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+.*?\[InsiderScan\] '
    r'🟢 内部人集群买入: (US\.\w+)'
)
# 匹配: [InsiderScan] 开始扫描 37 只标的...
SCAN_START_PATTERN = re.compile(
    r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+.*?\[InsiderScan\] 开始扫描 (\d+) 只标的'
)
# 匹配: [InsiderScan] 扫描完成, 未发现集群买入信号
SCAN_EMPTY_PATTERN = re.compile(
    r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+.*?\[InsiderScan\] 扫描完成'
)


def _iter_log_lines(log_files):
    """迭代所有日志文件的行 (支持 .gz)"""
    for lf in log_files:
        if not lf.exists():
            continue
        if lf.suffix == '.gz':
            try:
                with gzip.open(lf, 'rt', encoding='utf-8', errors='replace') as f:
                    yield from f
            except Exception:
                continue
        else:
            with open(lf, 'r', encoding='utf-8', errors='replace') as f:
                yield from f


def _get_log_files(days_back=2):
    """获取最近N天的日志文件 (当前+轮转)"""
    files = [CURRENT_LOG]
    for f in LOG_DIR.glob("ftseek_us.log-*"):
        files.append(f)
    return files


def scan_today():
    """扫描今日 insider 信号"""
    cutoff = datetime.now() - timedelta(days=1)

    log_files = _get_log_files()

    signals = []
    scan_events = []
    scan_empty = []

    for line in _iter_log_lines(log_files):
        # 信号匹配
        m = SIGNAL_PATTERN.search(line)
        if m:
            ts_str, symbol = m.groups()
            ts = datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S')
            if ts >= cutoff:
                signals.append({
                    'timestamp': ts.isoformat(),
                    'symbol': symbol,
                    'raw': line.strip()
                })
            continue

        # 扫描开始
        m = SCAN_START_PATTERN.search(line)
        if m:
            ts_str, count = m.groups()
            ts = datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S')
            if ts >= cutoff:
                scan_events.append({
                    'timestamp': ts.isoformat(),
                    'symbols_count': int(count)
                })
            continue

        # 空扫描
        m = SCAN_EMPTY_PATTERN.search(line)
        if m:
            ts_str = m.group(1)
            ts = datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S')
            if ts >= cutoff:
                scan_empty.append(ts.isoformat())
            continue

    # 从 JSONL 补充
    if SIGNAL_LOG.exists():
        with open(SIGNAL_LOG, 'r', errors='replace') as f:
            for line in f:
                try:
                    sig = json.loads(line.strip())
                    ts = datetime.fromisoformat(sig['timestamp'])
                    if ts >= cutoff:
                        signals.append(sig)
                except (json.JSONDecodeError, KeyError):
                    continue

    # 去重 (按 timestamp + symbol)
    seen = set()
    unique_signals = []
    for s in signals:
        key = (s.get('timestamp', ''), s.get('symbol', ''))
        if key not in seen:
            seen.add(key)
            unique_signals.append(s)

    # 统计唯一标的
    symbols = sorted(set(s['symbol'] for s in unique_signals))

    # 生成报告
    report = f"📊 Insider Signal Daily Report - {datetime.now().strftime('%Y-%m-%d')} (UTC)\n"
    report += f"   Cutoff: {cutoff.strftime('%Y-%m-%d %H:%M')} UTC\n"
    report += f"---\n"
    report += f"信号生成: {len(unique_signals)} 个\n"
    report += f"唯一标的: {len(symbols)} 只\n"
    if symbols:
        report += f"标的列表: {', '.join(symbols)}\n"
    report += f"扫描事件: {len(scan_events)} 次\n"
    if scan_events:
        report += f"扫描标的数: {scan_events[-1].get('symbols_count', '?')}\n"
    report += f"空扫描: {len(scan_empty)} 次\n"

    if unique_signals:
        report += "\n信号详情:\n"
        for s in unique_signals:
            report += f"  [{s['timestamp']}] {s['symbol']}\n"

    if not scan_events and not unique_signals:
        report += "\n⚠ 今日无扫描记录 — 调度器可能在扫描窗口(ET 06:00-10:00)内未运行\n"

    return report


if __name__ == '__main__':
    print(scan_today())
