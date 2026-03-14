#!/usr/bin/env python3
"""
FTSeek 模拟盘观测面板

实时显示模拟盘交易状态、收益率、持仓等信息

使用方式:
    python3 scripts/demo_dashboard.py
"""
import sys
import os
import sqlite3
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.performance.tracker import get_performance_tracker

def print_header(title: str):
    """打印标题"""
    print("\n" + "=" * 70)
    print(f"  {title}")
    print("=" * 70)

def print_section(title: str):
    """打印小标题"""
    print(f"\n📊 {title}")
    print("-" * 70)

def get_db_connection():
    """获取数据库连接"""
    db_path = '/opt/ftseek/FTSeek/data/ftseek.db'
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn

def show_portfolio_summary():
    """显示投资组合总览"""
    print_header("🎯 FTSeek 模拟盘观测面板")
    print(f"更新时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"交易模式：📝 模拟盘 (Paper Trading)")
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 总交易统计
    cursor.execute("""
        SELECT 
            COUNT(*) as total_trades,
            SUM(CASE WHEN return_rate > 0 THEN 1 ELSE 0 END) as winning_trades,
            SUM(CASE WHEN return_rate < 0 THEN 1 ELSE 0 END) as losing_trades,
            AVG(return_rate) as avg_return,
            SUM(profit) as total_profit
        FROM trade_pairs
    """)
    row = cursor.fetchone()
    
    if row and row['total_trades']:
        total = row['total_trades']
        wins = row['winning_trades'] or 0
        losses = row['losing_trades'] or 0
        win_rate = (wins / total * 100) if total > 0 else 0
        avg_return = row['avg_return'] or 0
        total_profit = row['total_profit'] or 0
        
        print_section("投资组合总览")
        print(f"  总交易数：    {total} 笔")
        print(f"  盈利交易：    {wins} 笔")
        print(f"  亏损交易：    {losses} 笔")
        print(f"  胜率：        {win_rate:.2f}%")
        print(f"  平均收益率：  {avg_return:.2f}%")
        print(f"  总收益：      {total_profit:+,.2f} HKD")
    else:
        print_section("投资组合总览")
        print("  暂无交易记录")
    
    conn.close()

def show_recent_trades():
    """显示最近交易"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT 
            code,
            buy_price,
            sell_price,
            quantity,
            return_rate,
            profit,
            close_time
        FROM trade_pairs
        ORDER BY close_time DESC
        LIMIT 10
    """)
    
    rows = cursor.fetchall()
    conn.close()
    
    print_section("最近交易 (Top 10)")
    
    if rows:
        print(f"  {'股票':<12} {'买入价':>10} {'卖出价':>10} {'数量':>8} {'收益率':>10} {'收益':>12} {'时间':>20}")
        print("  " + "-" * 86)
        
        for row in rows:
            code = row['code'].replace('HK.', '')
            profit_icon = "📈" if row['profit'] > 0 else "📉" if row['profit'] < 0 else "➖"
            close_time = row['close_time'].split('T')[0] if 'T' in row['close_time'] else row['close_time'][:10]
            
            print(f"  {code:<12} {row['buy_price']:>10.2f} {row['sell_price']:>10.2f} {row['quantity']:>8} {row['return_rate']:>9.2f}% {profit_icon} {row['profit']:>+10,.2f} {close_time:>20}")
    else:
        print("  暂无交易记录")

def show_stock_performance():
    """显示各股票表现"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT 
            code,
            COUNT(*) as trades,
            AVG(return_rate) as avg_return,
            SUM(profit) as total_profit,
            SUM(CASE WHEN profit > 0 THEN 1 ELSE 0 END) as wins
        FROM trade_pairs
        GROUP BY code
        ORDER BY total_profit DESC
    """)
    
    rows = cursor.fetchall()
    conn.close()
    
    print_section("各股票表现")
    
    if rows:
        print(f"  {'股票':<12} {'交易次数':>10} {'胜率':>10} {'平均收益':>12} {'总收益':>15}")
        print("  " + "-" * 64)
        
        for row in rows:
            code = row['code'].replace('HK.', '')
            win_rate = (row['wins'] / row['trades'] * 100) if row['trades'] > 0 else 0
            profit_icon = "📈" if row['total_profit'] > 0 else "📉" if row['total_profit'] < 0 else "➖"
            
            print(f"  {code:<12} {row['trades']:>10} {win_rate:>9.1f}% {row['avg_return']:>+11.2f}% {profit_icon} {row['total_profit']:>+13,.2f}")
    else:
        print("  暂无交易记录")

def show_pending_signals():
    """显示待确认信号"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT 
            id,
            code,
            name,
            action,
            price,
            quantity,
            strategy,
            created_at
        FROM signals
        WHERE status IN ('pending', 'pending_confirmation')
        ORDER BY created_at DESC
        LIMIT 10
    """)
    
    rows = cursor.fetchall()
    conn.close()
    
    print_section("待确认信号")
    
    if rows:
        for row in rows:
            icon = "🟢" if row['action'] == 'buy' else "🔴"
            code = row['code'].replace('HK.', '')
            created = row['created_at'].split('T')[0] if 'T' in row['created_at'] else row['created_at'][:10]
            
            print(f"\n  {icon} {row['name']} ({code})")
            print(f"     方向：{'买入' if row['action'] == 'buy' else '卖出'}")
            print(f"     价格：{row['price']:.2f} HKD")
            print(f"     数量：{row['quantity']} 股")
            print(f"     策略：{row['strategy']}")
            print(f"     时间：{created}")
            print(f"     确认命令:")
            print(f"     python3 scripts/trade_executor.py --signal-id {row['id']} --action {row['action'].upper()} --mode paper")
    else:
        print("  暂无待确认信号")

def show_strategy_performance():
    """显示策略表现"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 从 signals 表统计各策略表现
    cursor.execute("""
        SELECT 
            strategy,
            COUNT(*) as signals,
            SUM(CASE WHEN status = 'executed' THEN 1 ELSE 0 END) as executed
        FROM signals
        WHERE strategy IS NOT NULL
        GROUP BY strategy
        ORDER BY executed DESC
    """)
    
    rows = cursor.fetchall()
    conn.close()
    
    print_section("策略表现")
    
    if rows:
        print(f"  {'策略':<20} {'信号数':>10} {'已执行':>10} {'执行率':>10}")
        print("  " + "-" * 54)
        
        for row in rows:
            exec_rate = (row['executed'] / row['signals'] * 100) if row['signals'] > 0 else 0
            print(f"  {row['strategy']:<20} {row['signals']:>10} {row['executed']:>10} {exec_rate:>9.1f}%")
    else:
        print("  暂无策略数据")

def show_observation_tips():
    """显示观测提示"""
    print_section("📝 观测提示")
    print("""
  1. 模拟盘用于验证策略和流程，不实际下单
  2. 关注胜率和平均收益率，而非单笔收益
  3. 定期查看各股票表现，优化选股策略
  4. 待确认信号请及时处理 (买入/卖出)
  5. 连续亏损时考虑调整策略参数
  
  常用命令:
  - 刷新面板：python3 scripts/demo_dashboard.py
  - 查询信号：python3 scripts/query_signals.py --status pending
  - 确认交易：python3 scripts/trade_executor.py --signal-id <ID> --action BUY/SELL --mode paper
  - 查看日志：tail -f logs/realtime_monitor.log
    """)

def main():
    """主函数"""
    # 显示各个面板
    show_portfolio_summary()
    show_recent_trades()
    show_stock_performance()
    show_pending_signals()
    show_strategy_performance()
    show_observation_tips()
    
    print("\n" + "=" * 70)
    print("  📊 数据截止时间：" + datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    print("=" * 70 + "\n")

if __name__ == "__main__":
    main()
