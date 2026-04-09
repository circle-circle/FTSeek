#!/usr/bin/env python3
"""
补充导入富途模拟交易成交记录 (第二批)
2026-03-30 11:15 截图数据
"""

import sqlite3
import uuid
from datetime import datetime

DB_PATH = "/opt/ftseek/FTSeek/data/ftseek.db"

# 从 11:15 截图识别的新成交记录 (10:30 之后)
NEW_TRADES = [
    # (代码，名称，买卖，数量，价格，时间)
    ("HK.09992", "泡泡玛特", "SELL", 400, 144.40, "2026-03-30 10:32:02"),
    ("HK.09992", "泡泡玛特", "BUY", 400, 144.50, "2026-03-30 10:32:03"),
    ("HK.00981", "中芯国际", "SELL", 1000, 49.82, "2026-03-30 10:45:09"),
    ("HK.00981", "中芯国际", "BUY", 1000, 49.84, "2026-03-30 10:45:10"),
    ("HK.03690", "美团-W", "SELL", 100, 83.70, "2026-03-30 10:45:37"),
    ("HK.09992", "泡泡玛特", "SELL", 400, 146.20, "2026-03-30 11:10:35"),
    ("HK.09992", "泡泡玛特", "BUY", 400, 146.30, "2026-03-30 11:10:36"),
    ("HK.00981", "中芯国际", "SELL", 1000, 50.80, "2026-03-30 11:13:47"),
    ("HK.00981", "中芯国际", "BUY", 1000, 50.85, "2026-03-30 11:13:48"),
    ("HK.09992", "泡泡玛特", "SELL", 400, 146.70, "2026-03-30 11:14:04"),
    ("HK.09992", "泡泡玛特", "BUY", 400, 146.80, "2026-03-30 11:14:06"),
]

def import_new_trades():
    """导入新成交记录到数据库"""
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    imported = 0
    skipped = 0
    
    for code, name, action, quantity, price, entry_time in NEW_TRADES:
        # 生成唯一 ID
        trade_id = f"trade_{entry_time.replace(' ', '_').replace(':', '')}_{code.replace('.', '')}"
        order_id = f"futu_{uuid.uuid4().hex[:12]}"
        
        # 计算盈亏 (简化：假设已平仓)
        pnl = 0
        pnl_percent = 0
        
        # 检查是否已存在
        cursor.execute("SELECT id FROM trades WHERE id = ?", (trade_id,))
        if cursor.fetchone():
            print(f"⏭️  跳过：{trade_id} (已存在)")
            skipped += 1
            continue
        
        # 插入数据库
        cursor.execute("""
            INSERT INTO trades (
                id, signal_id, code, name, action,
                entry_price, exit_price, quantity,
                pnl, pnl_percent, fees,
                strategy, entry_time, exit_time,
                exit_reason, order_id, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            trade_id,
            f"manual_{trade_id}",
            code,
            name,
            action,
            price,
            None,
            quantity,
            pnl,
            pnl_percent,
            0,
            'manual_import',
            entry_time,
            None,
            None,
            order_id,
            'OPEN' if action == 'BUY' else 'CLOSED'
        ))
        
        imported += 1
        print(f"✅ 导入：{code} {action} {quantity}股 @ {price} ({entry_time})")
    
    conn.commit()
    
    # 验证导入结果
    cursor.execute("""
        SELECT COUNT(*) FROM trades 
        WHERE date(entry_time) = date('now')
    """)
    today_count = cursor.fetchone()[0]
    
    # 统计各股票成交
    cursor.execute("""
        SELECT code, name, COUNT(*) as cnt, SUM(quantity) as vol
        FROM trades 
        WHERE date(entry_time) = date('now')
        GROUP BY code, name
        ORDER BY cnt DESC
    """)
    stock_stats = cursor.fetchall()
    
    print(f"\n{'='*60}")
    print(f"第二批导入完成!")
    print(f"  本次导入：{imported} 条")
    print(f"  跳过：{skipped} 条")
    print(f"  今日总成交：{today_count} 条")
    print(f"\n各股票成交统计:")
    for row in stock_stats:
        print(f"  {row[0]} {row[1]}: {row[2]}笔 / {row[3]}股")
    print(f"{'='*60}")
    
    conn.close()
    
    return imported

if __name__ == '__main__':
    print(f"开始补充导入 2026-03-30 第二批成交记录...")
    print(f"{'='*60}")
    
    imported = import_new_trades()
    
    if imported > 0:
        print(f"\n✅ 成功导入 {imported} 条新成交记录")
    else:
        print(f"\n⚠️ 没有导入新记录")
