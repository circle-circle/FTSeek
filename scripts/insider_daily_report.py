#!/usr/bin/env python3
"""Insider Signal Monitor - 每日信号质量报告"""
import sys
sys.path.insert(0, '/opt/ftseek/FTSeek-US')
from pathlib import Path
import json, re
from datetime import datetime, timedelta
from collections import defaultdict

LOG_FILE = Path('/opt/ftseek/FTSeek-US/logs/ftseek_us.log')
STATS_FILE = Path('/opt/ftseek/FTSeek-US/data/stats/insider_cumulative.json')

def scan_today():
    cutoff = datetime.now() - timedelta(days=1)
    signals = []
    pattern = re.compile(r'\[InsiderScan\].*?(?:集群买入|no cluster|扫描完成|发现)')
    
    if not LOG_FILE.exists():
        return "日志文件不存在"
    
    insider_lines = []
    with open(LOG_FILE, 'r', encoding='utf-8', errors='replace') as f:
        for line in f:
            if '[InsiderScan]' in line:
                ts_match = re.match(r'(\d{4}-\d{2}-\d{2})', line)
                if ts_match:
                    log_date = datetime.strptime(ts_match.group(1), '%Y-%m-%d')
                    if log_date >= cutoff:
                        insider_lines.append(line.strip())
    
    if not insider_lines:
        return "今日无 InsiderScan 日志（可能假日未运行）"
    
    # 统计
    cluster_found = [l for l in insider_lines if '集群买入' in l or '发现' in l]
    scan_done = [l for l in insider_lines if '扫描完成' in l or '开始扫描' in l]
    
    report = f"Insider Signal Daily Report - {datetime.now().strftime('%Y-%m-%d')}\n"
    report += f"扫描日志行数: {len(insider_lines)}\n"
    report += f"扫描事件: {len(scan_done)}\n"
    report += f"集群买入信号: {len(cluster_found)}\n"
    
    if cluster_found:
        report += "\n信号详情:\n"
        for l in cluster_found:
            report += f"  {l}\n"
    
    return report

if __name__ == '__main__':
    print(scan_today())
