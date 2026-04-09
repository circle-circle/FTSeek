#!/usr/bin/env python3
"""
手动导入富途模拟交易成交记录到数据库
2026-03-30 紧急数据同步
"""

import sqlite3
import uuid
from datetime import datetime

DB_PATH = "/opt/ftseek/FTSeek/data/ftseek.db"

# 从截图识别的今日成交记录
TODAY_TRADES = [
    # (代码，名称，买卖，数量，价格，时间)
    ("HK.09992", "泡泡玛特", "BUY", 400, 142.60, "2026-03-30 09:48:03"),
    ("HK.09992", "泡泡玛特", "SELL", 400, 142.50, "2026-03-30 09:48:03"),
    ("HK.03690", "美团-W", "BUY", 200, 84.35, "2026-03-30 09:49:44"),
    ("HK.03690", "美团-W", "SELL", 200, 84.30, "2026-03-30 09:49:43"),
    ("HK.09992", "泡泡玛特", "BUY", 400, 143.10, "2026-03-30 09:51:12"),
    ("HK.09992", "泡泡玛特", "SELL", 400, 143.00, "2026-03-30 09:51:10"),
    ("HK.09992", "泡泡玛特", "BUY", 400, 143.00, "2026-03-30 09:53:16"),
    ("HK.09992", "泡泡玛特", "SELL", 400, 143.00, "2026-03-30 09:53:15"),
    ("HK.03690", "美团-W", "BUY", 100, 84.35, "2026-03-30 10:07:46"),
]

def import_trades():
    """导入成交记录到数据库"""
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    imported = 0
    skipped = 0
    
    for code, name, action, quantity, price, entry_time in TODAY_TRADES:
        # 生成唯一 ID
        trade_id = f"trade_{entry_time.replace(' ', '_').replace(':', '')}_{code.replace('.', '')}"
        order_id = f"futu_{uuid.uuid4().hex[:12]}"
        
        # 计算盈亏 (简化：假设已平仓，实际需要根据持仓计算)
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
            f"manual_{trade_id}",  # 手动导入标记
            code,
            name,
            action,
            price,
            None,  # exit_price (未平仓)
            quantity,
            pnl,
            pnl_percent,
            0,  # fees
            'manual_import',  # strategy
            entry_time,
            None,  # exit_time
            None,  # exit_reason
            order_id,
            'OPEN' if action == 'BUY' else 'CLOSED'  # 买入为持仓，卖出为已平仓
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
    
    print(f"\n{'='*60}")
    print(f"导入完成!")
    print(f"  本次导入：{imported} 条")
    print(f"  跳过：{skipped} 条")
    print(f"  今日总成交：{today_count} 条")
    print(f"{'='*60}")
    
    conn.close()
    
    return imported

if __name__ == '__main__':
    print(f"开始导入 2026-03-30 成交记录...")
    print(f"{'='*60}")
    
    imported = import_trades()
    
    if imported > 0:
        print(f"\n✅ 成功导入 {imported} 条成交记录到数据库")
    else:
        print(f"\n⚠️ 没有导入新记录")
