#!/usr/bin/env python3
"""
交易记录统一查询脚本

用法:
    python3 query_trades.py --today          # 查询今日交易
    python3 query_trades.py --week           # 查询本周交易
    python3 query_trades.py --month          # 查询本月交易
    python3 query_trades.py --symbol AAPL    # 查询指定股票
    python3 query_trades.py --all            # 查询所有交易
"""

import argparse
import sqlite3
import json
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "ftseek_us.db"

def get_connection():
    """获取数据库连接"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def query_trades(symbol=None, days=7):
    """
    查询交易记录
    
    Args:
        symbol: 股票代码 (可选)
        days: 查询最近 N 天
    
    Returns:
        交易记录列表
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    # 构建查询
    query = """
        SELECT 
            trade_date,
            symbol,
            direction,
            quantity,
            entry_price,
            exit_price,
            realized_pnl,
            pnl_pct,
            exit_reason,
            futu_order_id,
            created_at
        FROM trade_records
        WHERE date(created_at) >= date('now', ?)
    """
    params = [f'-{days} days']
    
    if symbol:
        query += " AND symbol = ?"
        params.append(f'US.{symbol}')
    
    query += " ORDER BY created_at DESC"
    
    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()
    
    # 转换为字典列表
    trades = []
    for row in rows:
        trades.append({
            'trade_date': row['trade_date'],
            'symbol': row['symbol'],
            'direction': row['direction'],
            'quantity': row['quantity'],
            'entry_price': row['entry_price'],
            'exit_price': row['exit_price'] if row['exit_price'] else '-',
            'realized_pnl': row['realized_pnl'] if row['realized_pnl'] else 0,
            'pnl_pct': row['pnl_pct'] if row['pnl_pct'] else 0,
            'exit_reason': row['exit_reason'],
            'futu_order_id': row['futu_order_id'],
            'created_at': row['created_at'],
        })
    
    return trades

def print_trades(trades):
    """打印交易记录"""
    if not trades:
        print("❌ 无交易记录")
        return
    
    print(f"\n{'='*100}")
    print(f"交易记录 (共 {len(trades)} 条)")
    print(f"{'='*100}\n")
    
    print(f"{'日期':<12} {'代码':<10} {'方向':<6} {'数量':<8} {'入场':<10} {'出场':<10} {'盈亏':<12} {'盈亏率':<10}")
    print(f"{'-'*100}")
    
    for trade in trades:
        pnl = trade['realized_pnl']
        pnl_pct = trade['pnl_pct']
        exit_price = trade['exit_price']
        
        pnl_str = f"${pnl:.2f}" if pnl != 0 else '-'
        pnl_pct_str = f"{pnl_pct:.2f}%" if pnl_pct != 0 else '-'
        
        print(f"{trade['trade_date']:<12} {trade['symbol']:<10} {trade['direction']:<6} {trade['quantity']:<8} "
              f"${trade['entry_price']:<9.2f} {exit_price:<9} {pnl_str:<11} {pnl_pct_str:<9}")
    
    print(f"\n{'='*100}\n")
    
    # 统计
    total_pnl = sum(t['realized_pnl'] for t in trades if t['realized_pnl'] != 0)
    total_trades = len(trades)
    winning_trades = sum(1 for t in trades if t['realized_pnl'] and t['realized_pnl'] > 0)
    win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0
    
    print(f"统计:")
    print(f"  总交易数：{total_trades}")
    print(f"  盈利交易：{winning_trades}")
    print(f"  亏损交易：{total_trades - winning_trades}")
    print(f"  胜率：{win_rate:.1f}%")
    print(f"  总盈亏：${total_pnl:.2f}")
    print()

def main():
    parser = argparse.ArgumentParser(description='交易记录统一查询')
    parser.add_argument('--today', action='store_true', help='查询今日交易')
    parser.add_argument('--week', action='store_true', help='查询本周交易')
    parser.add_argument('--month', action='store_true', help='查询本月交易')
    parser.add_argument('--symbol', type=str, help='股票代码 (如 AAPL)')
    parser.add_argument('--json', action='store_true', help='JSON 格式输出')
    parser.add_argument('--all', action='store_true', help='查询所有交易')
    
    args = parser.parse_args()
    
    # 确定查询天数
    if args.today:
        days = 1
    elif args.week:
        days = 7
    elif args.month:
        days = 30
    elif args.all:
        days = 365
    else:
        days = 7  # 默认 7 天
    
    # 查询
    trades = query_trades(
        symbol=args.symbol.upper() if args.symbol else None,
        days=days
    )
    
    # 输出
    if args.json:
        print(json.dumps(trades, indent=2, ensure_ascii=False))
    else:
        print_trades(trades)

if __name__ == "__main__":
    main()
