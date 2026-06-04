#!/usr/bin/env python3
"""
实时交易跟踪器 - 今晚 ORB + 主线策略监控
每 2 分钟检查新交易，输出实时统计
"""

import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path("data/ftseek_us.db")
LOG_FILE = Path("logs/tonight_trades_realtime.log")

def get_recent_trades(hours=12):
    """获取最近 N 小时的交易"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()

    cursor.execute("""
        SELECT
            symbol, direction, entry_time, exit_time,
            entry_price, exit_price, quantity, realized_pnl,
            exit_reason, strategy_id, source_type
        FROM trade_records
        WHERE entry_time >= ?
        AND realized_pnl IS NOT NULL
        ORDER BY entry_time DESC
    """, (cutoff,))

    trades = cursor.fetchall()
    conn.close()
    return trades

def calculate_stats(trades):
    """计算统计"""
    if not trades:
        return None

    total = len(trades)
    wins = [t for t in trades if t[7] > 0]
    losses = [t for t in trades if t[7] < 0]

    win_rate = len(wins) / total * 100 if total > 0 else 0
    total_pnl = sum(t[7] for t in trades)
    avg_pnl = total_pnl / total if total > 0 else 0

    # 按策略分组
    orb_trades = [t for t in trades if t[9] == 'orb' or t[10] == 'ORB' or 'ORB' in (t[8] or '')]
    main_trades = [t for t in trades if t not in orb_trades]

    return {
        'total': total,
        'wins': len(wins),
        'losses': len(losses),
        'win_rate': win_rate,
        'total_pnl': total_pnl,
        'avg_pnl': avg_pnl,
        'orb_count': len(orb_trades),
        'main_count': len(main_trades),
    }

def log_message(msg):
    """写入日志"""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    log_line = f"[{timestamp}] {msg}"
    print(log_line)

    with open(LOG_FILE, 'a') as f:
        f.write(log_line + '\n')

def main():
    log_message("=" * 70)
    log_message("实时交易跟踪器启动")
    log_message("=" * 70)

    last_trade_count = 0
    check_interval = 120  # 2分钟检查一次

    try:
        while True:
            trades = get_recent_trades(hours=12)
            stats = calculate_stats(trades)

            if stats and stats['total'] > last_trade_count:
                new_count = stats['total'] - last_trade_count
                log_message(f"🔔 发现 {new_count} 笔新交易！")

                # 显示新交易详情
                for trade in trades[:new_count]:
                    symbol, direction, entry_time, exit_time, entry_px, exit_px, qty, pnl, exit_reason, strategy, source = trade
                    pnl_str = f"+${pnl:.2f}" if pnl > 0 else f"-${abs(pnl):.2f}"
                    log_message(f"  {symbol} {direction} | {pnl_str} | {exit_reason} | {strategy or source}")

                # 显示累计统计
                log_message(f"📊 累计统计:")
                log_message(f"  总交易: {stats['total']} | 胜率: {stats['win_rate']:.1f}% | 总PnL: ${stats['total_pnl']:.2f}")
                log_message(f"  ORB: {stats['orb_count']}笔 | 主线: {stats['main_count']}笔")

                last_trade_count = stats['total']

            elif stats and stats['total'] > 0:
                # 每10分钟输出一次状态
                if int(time.time()) % 600 < check_interval:
                    log_message(f"⏱️  状态: {stats['total']}笔 | 胜率: {stats['win_rate']:.1f}% | PnL: ${stats['total_pnl']:.2f}")

            time.sleep(check_interval)

    except KeyboardInterrupt:
        log_message("监控已停止")

if __name__ == "__main__":
    main()
