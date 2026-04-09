#!/usr/bin/env python3
"""
重新导入 2026-03-30 成交记录
基于用户确认的 14 条实际成交 + 新订单
"""

import sqlite3
import uuid
from datetime import datetime

DB_PATH = "/opt/ftseek/FTSeek/data/ftseek.db"

# 清空今日手动导入的数据
def clear_today_data():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM trades WHERE date(entry_time) = date('now') AND strategy = 'manual_import'")
    conn.commit()
    conn.close()
    print("✅ 已清空今日手动导入的数据")

# 从订单截图识别的数据（需要用户确认）
# 格式：(代码，名称，买卖，数量，价格，时间，状态)
ORDERS = [
    # 之前导入的 14 条成交 (待用户确认)
    # 新增订单 (从 11:25 截图)
    ("HK.00981", "中芯国际", "BUY", 1000, 51.30, "2026-03-30 11:25:41", "FILLED"),
    ("HK.00981", "中芯国际", "SELL", 1000, 51.25, "2026-03-30 11:25:41", "FILLED"),
    ("HK.03690", "美团-W", "BUY", 200, 85.10, "2026-03-30 11:16:45", "FILLED"),
    ("HK.03690", "美团-W", "SELL", 200, 85.10, "2026-03-30 11:15:39", "FILLED"),
]

def import_orders():
    """导入订单到数据库"""
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    imported = 0
    
    for code, name, action, quantity, price, entry_time, status in ORDERS:
        trade_id = f"trade_{entry_time.replace(' ', '_').replace(':', '')}_{code.replace('.', '')}"
        order_id = f"futu_{uuid.uuid4().hex[:12]}"
        
        # 检查是否已存在
        cursor.execute("SELECT id FROM trades WHERE id = ?", (trade_id,))
        if cursor.fetchone():
            print(f"⏭️  跳过：{trade_id} (已存在)")
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
            0,
            0,
            0,
            'manual_import',
            entry_time,
            None,
            None,
            order_id,
            'OPEN' if action == 'BUY' and status == 'FILLED' else 'CLOSED'
        ))
        
        imported += 1
        print(f"✅ 导入：{code} {action} {quantity}股 @ {price} ({entry_time})")
    
    conn.commit()
    
    # 统计
    cursor.execute("""
        SELECT code, name, COUNT(*) as cnt
        FROM trades 
        WHERE date(entry_time) = date('now') AND strategy = 'manual_import'
        GROUP BY code, name
    """)
    stats = cursor.fetchall()
    
    cursor.execute("SELECT COUNT(*) FROM trades WHERE date(entry_time) = date('now') AND strategy = 'manual_import'")
    total = cursor.fetchone()[0]
    
    print(f"\n{'='*60}")
    print(f"导入完成!")
    print(f"  本次导入：{imported} 条")
    print(f"  今日总成交：{total} 条")
    print(f"\n各股票统计:")
    for row in stats:
        print(f"  {row[0]} {row[1]}: {row[2]}笔")
    print(f"{'='*60}")
    
    conn.close()
    
    return imported

if __name__ == '__main__':
    print(f"开始重新导入 2026-03-30 成交记录...")
    print(f"{'='*60}")
    
    # 清空
    clear_today_data()
    
    # 导入
    imported = import_orders()
    
    print(f"\n✅ 完成")
