#!/usr/bin/env python3
"""
ORB 今晚交易实时监控脚本 (2026-06-01 晚盘)

功能:
1. 实时监控 ORB 信号和交易
2. 记录所有 ORB 交易到独立日志
3. 计算实时胜率和 PnL
4. 异常告警
"""

import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict

DB_PATH = Path("data/ftseek_us.db")
LOG_FILE = Path("logs/orb_tonight_trades.log")
STATS_FILE = Path("logs/orb_tonight_stats.txt")

def get_orb_trades_today():
    """获取今天的所有 ORB 交易"""
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()

    # 查找 ORB 策略的交易
    cursor.execute("""
        SELECT
            symbol,
            direction,
            entry_time,
            exit_time,
            entry_price,
            exit_price,
            quantity,
            realized_pnl,
            exit_reason,
            signal_confidence,
            signal_strength,
            market_regime
        FROM trade_records
        WHERE DATE(entry_time) = DATE('now', 'localtime')
          AND (strategy_id = 'orb' OR source_type = 'ORB' OR entry_reason LIKE '%ORB%')
        ORDER BY entry_time DESC
    """)

    trades = cursor.fetchall()
    conn.close()
    return trades

def get_open_orb_positions():
    """获取当前 ORB 持仓"""
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            symbol,
            direction,
            size,
            avg_entry_price,
            trace_id,
            strategy_id
        FROM open_positions
        WHERE strategy_id = 'orb'
          AND (sync_status IS NULL OR sync_status NOT IN
              ('closed_by_daemon', 'closed_by_scheduler', 'closed_by_sync',
               'closed_emergency', 'stale_not_in_broker'))
    """)

    positions = cursor.fetchall()
    conn.close()
    return positions

def monitor_orb_logs():
    """监控 ORB 日志文件"""
    log_file = Path("logs/ftseek_us.log")
    if not log_file.exists():
        return []

    with open(log_file, 'r') as f:
        lines = f.readlines()

    # 获取最近 100 行包含 ORB 的日志
    orb_lines = [line for line in lines[-200:] if '[ORB]' in line]
    return orb_lines[-20:]  # 返回最近 20 行

def calculate_stats(trades):
    """计算交易统计"""
    if not trades:
        return {
            'total': 0,
            'wins': 0,
            'losses': 0,
            'win_rate': 0.0,
            'total_pnl': 0.0,
            'avg_pnl': 0.0,
            'max_win': 0.0,
            'max_loss': 0.0,
        }

    wins = [t for t in trades if t[7] and t[7] > 0]
    losses = [t for t in trades if t[7] and t[7] < 0]
    pnls = [t[7] for t in trades if t[7]]

    return {
        'total': len(trades),
        'wins': len(wins),
        'losses': len(losses),
        'win_rate': len(wins) / len(trades) * 100 if trades else 0.0,
        'total_pnl': sum(pnls) if pnls else 0.0,
        'avg_pnl': sum(pnls) / len(pnls) if pnls else 0.0,
        'max_win': max(pnls) if pnls else 0.0,
        'max_loss': min(pnls) if pnls else 0.0,
    }

def write_stats(stats, positions, recent_logs):
    """写入统计信息到文件"""
    with open(STATS_FILE, 'w') as f:
        f.write("=" * 70 + "\n")
        f.write(f"ORB 今晚交易统计 (更新时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')})\n")
        f.write("=" * 70 + "\n\n")

        f.write("📊 交易统计:\n")
        f.write(f"  总交易数: {stats['total']}\n")
        f.write(f"  盈利交易: {stats['wins']}\n")
        f.write(f"  亏损交易: {stats['losses']}\n")
        f.write(f"  胜率: {stats['win_rate']:.1f}%\n")
        f.write(f"  总 PnL: ${stats['total_pnl']:.2f}\n")
        f.write(f"  平均 PnL: ${stats['avg_pnl']:.2f}\n")
        f.write(f"  最大盈利: ${stats['max_win']:.2f}\n")
        f.write(f"  最大亏损: ${stats['max_loss']:.2f}\n\n")

        f.write("📈 当前持仓:\n")
        if positions:
            for pos in positions:
                symbol, direction, size, entry_price, trace_id, strategy = pos
                f.write(f"  {symbol} {direction} {size}股 @ ${entry_price:.2f} (trace: {trace_id})\n")
        else:
            f.write("  无当前持仓\n")
        f.write("\n")

        f.write("📝 最近 ORB 日志 (最近 10 行):\n")
        for line in recent_logs[-10:]:
            f.write(f"  {line.strip()}\n")
        f.write("\n")

        f.write("=" * 70 + "\n")

def main():
    print("=" * 70)
    print("ORB 今晚交易实时监控")
    print(f"启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)
    print()

    last_trade_count = 0
    check_count = 0

    try:
        while True:
            check_count += 1

            # 获取交易和持仓
            trades = get_orb_trades_today()
            positions = get_open_orb_positions()
            recent_logs = monitor_orb_logs()

            # 计算统计
            stats = calculate_stats(trades)

            # 检查新交易
            current_trade_count = stats['total']
            if current_trade_count > last_trade_count:
                new_trades = current_trade_count - last_trade_count
                print(f"\n🔔 发现 {new_trades} 笔新 ORB 交易!")
                for trade in trades[:new_trades]:
                    symbol, direction, entry_time, exit_time, entry_price, exit_price, qty, pnl, exit_reason, conf, strength, regime = trade
                    pnl_str = f"+${pnl:.2f}" if pnl and pnl > 0 else f"-${abs(pnl):.2f}" if pnl else "$0.00"
                    print(f"  {symbol} {direction} | PnL: {pnl_str} | 退出: {exit_reason or '持仓中'}")
                last_trade_count = current_trade_count

            # 每 10 次检查打印一次状态
            if check_count % 10 == 0:
                print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 第 {check_count} 次检查")
                print(f"  交易数: {stats['total']} | 胜率: {stats['win_rate']:.1f}% | PnL: ${stats['total_pnl']:.2f}")
                print(f"  持仓数: {len(positions)}")

                if positions:
                    print(f"  当前持仓:")
                    for pos in positions:
                        symbol, direction, size, entry_price, trace_id, strategy = pos
                        print(f"    {symbol} {direction} {size}股 @ ${entry_price:.2f}")

            # 写入统计文件
            write_stats(stats, positions, recent_logs)

            # 等待 30 秒
            time.sleep(30)

    except KeyboardInterrupt:
        print("\n\n监控已停止")
        print(f"最终统计已写入: {STATS_FILE}")

if __name__ == "__main__":
    main()
