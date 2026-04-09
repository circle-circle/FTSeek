#!/usr/bin/env python3
"""
FTSeek V3 功能监控脚本

监控内容:
1. 新功能拦截统计
2. 持仓状态
3. 当日交易统计
4. 服务健康状态
"""

import json
import sqlite3
from datetime import datetime
from collections import defaultdict
import os

DB_PATH = '/opt/ftseek/FTSeek/data/ftseek.db'
SIGNAL_PATH = '/opt/ftseek/FTSeek/data/signal_records/signals_2026-03-25.json'
MONITOR_PATH = '/opt/ftseek/FTSeek/data/monitoring/monitor_2026-03-25.json'

def clear_screen():
    os.system('clear' if os.name == 'posix' else 'cls')

def main():
    clear_screen()
    print("=" * 80)
    print(f"FTSeek V3 功能监控 | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)
    print()
    
    # 1. 读取信号数据
    try:
        with open(SIGNAL_PATH, 'r') as f:
            signals = json.load(f)
    except:
        signals = []
    
    # 2. 统计信号
    total = len(signals)
    executed = [s for s in signals if s.get('executed')]
    rejected = [s for s in signals if s.get('reject_reason')]
    
    # 3. 拒绝原因分析
    reject_reasons = defaultdict(int)
    for s in rejected:
        r = s.get('reject_reason', '')
        if 'existing_position' in r:
            reject_reasons['已有持仓'] += 1
        elif '碎股' in r or '不足1手' in r:
            reject_reasons['碎股限制'] += 1
        elif 'weak_signal' in r.lower():
            reject_reasons['弱信号'] += 1
        elif 'trend_filter' in r.lower() or '趋势过滤' in r:
            reject_reasons['趋势过滤'] += 1
        elif '价格检查' in r or '追高' in r or '杀跌' in r:
            reject_reasons['价格检查'] += 1
        elif '持仓数' in r or '最大持仓' in r:
            reject_reasons['持仓限制'] += 1
        elif 'cooldown' in r.lower():
            reject_reasons['冷却中'] += 1
        else:
            reject_reasons['其他'] += 1
    
    # 4. 交易统计
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT COUNT(*), SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END),
               SUM(CASE WHEN realized_pnl < 0 THEN 1 ELSE 0 END),
               SUM(realized_pnl)
        FROM positions 
        WHERE status = 'CLOSED' AND date(entry_time) = '2026-03-25'
    ''')
    trade_stats = cursor.fetchone()
    
    cursor.execute('''
        SELECT id, code, action, quantity, entry_price, stop_loss, take_profit
        FROM positions 
        WHERE status = 'OPEN'
        ORDER BY id DESC
    ''')
    open_positions = cursor.fetchall()
    conn.close()
    
    # 输出统计
    print("📊 今日交易统计")
    print("-" * 80)
    print(f"总信号: {total} | 已执行: {len(executed)} ({len(executed)/total*100:.1f}%) | 已拒绝: {len(rejected)}")
    print()
    
    if trade_stats[0]:
        closed = trade_stats[0]
        wins = trade_stats[1] or 0
        losses = trade_stats[2] or 0
        pnl = trade_stats[3] or 0
        win_rate = wins / closed * 100 if closed > 0 else 0
        print(f"已平仓: {closed} | 胜率: {win_rate:.1f}% ({wins}胜/{losses}负)")
        print(f"总盈亏: {pnl:+.2f} HKD")
    print()
    
    print("🚫 拒绝原因分布")
    print("-" * 80)
    for reason, count in sorted(reject_reasons.items(), key=lambda x: -x[1]):
        bar = '█' * min(count, 20)
        print(f"  {reason:10} {count:3} {bar}")
    print()
    
    print("✅ V3 新功能效果")
    print("-" * 80)
    trend_count = reject_reasons.get('趋势过滤', 0)
    price_count = reject_reasons.get('价格检查', 0)
    position_count = reject_reasons.get('持仓限制', 0)
    
    print(f"  趋势过滤拦截: {trend_count} 次 {'✅ 已生效' if trend_count > 0 else '⏳ 等待触发'}")
    print(f"  价格检查拦截: {price_count} 次 {'✅ 已生效' if price_count > 0 else '⏳ 等待触发'}")
    print(f"  持仓限制拦截: {position_count} 次 {'✅ 已生效' if position_count > 0 else '⏳ 等待触发'}")
    print()
    
    print("📈 当前持仓")
    print("-" * 80)
    if open_positions:
        for pos in open_positions:
            id_, code, action, qty, entry, sl, tp = pos
            sl_val = sl if sl else 0
            tp_val = tp if tp else 0
            print(f"  {code}: {qty}股 @ {entry:.2f} | 止损:{sl_val:.2f} 止盈:{tp_val:.2f}")
    else:
        print("  无持仓")
    print()
    
    print("=" * 80)
    print("按 Ctrl+C 退出监控")
    print("=" * 80)

if __name__ == '__main__':
    import time
    try:
        while True:
            main()
            time.sleep(30)  # 每30秒刷新一次
    except KeyboardInterrupt:
        print("\n监控已停止")