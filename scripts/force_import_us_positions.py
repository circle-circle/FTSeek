#!/usr/bin/env python3
"""
强制导入美股真实持仓 (Force Import US Positions)
用于修复 DB 与 Futu 严重脱节的情况。
"""
import sys
sys.path.insert(0, '.')
import sqlite3
from futu import OpenUSTradeContext, TrdEnv
from app.config import OPEND_HOST, OPEND_PORT
from datetime import datetime

DB_PATH = 'data/ftseek.db'

print("🚀 正在强制导入美股真实持仓...")

# 1. 获取 Futu 真实持仓
ctx = OpenUSTradeContext(OPEND_HOST, OPEND_PORT)
ret, data = ctx.position_list_query(trd_env=TrdEnv.SIMULATE)
ctx.close()

if ret != 0 or data.empty:
    print("❌ 获取 Futu 持仓失败")
    sys.exit(1)

conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

for _, row in data.iterrows():
    code = str(row['code'])
    qty = float(row.get('qty', 0))
    cost = float(row.get('cost_price', 0))
    
    if qty == 0: continue
    
    action = 'SELL' if row.get('side', 'BUY') == 'SELL' or (qty < 0) else 'BUY' # Futu 做空通常 qty 为正，side 为 SELL
    
    # 检查 DB 是否已有 OPEN 记录
    cursor.execute("SELECT count(*) FROM positions WHERE code=? AND status='OPEN' AND quantity=?", (code, int(qty)))
    if cursor.fetchone()[0] > 0:
        print(f"ℹ️  {code} 已存在 OPEN 记录，跳过")
        continue
        
    # 检查是否有 CLOSED 记录（可能有旧数据干扰），这里直接 Insert 新的 OPEN
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    # 插入
    cursor.execute("""
        INSERT INTO positions (
            code, action, quantity, entry_price, entry_time, status, 
            market, strategy, signal_id, trader_type, unrealized_pnl, realized_pnl
        ) VALUES (?, ?, ?, ?, ?, 'OPEN', 'US', 'force_import', NULL, 'MANUAL', ?, 0)
    """, (
        code, action, int(abs(qty)), cost, now_str, 
        float(row.get('pl_val', 0)) # 当前盈亏作为初始值
    ))
    
    print(f"✅ 导入成功: {code} | {action} {abs(int(qty))} @ {cost}")

conn.commit()
conn.close()
print("🏁 导入完成")
