#!/usr/bin/env python3
"""
P0 修复：修复 trades 表和 positions 表数据不一致问题

使用方法:
    python3 scripts/fix_db_inconsistency.py
"""
import sqlite3
from datetime import datetime
import sys
import os

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config

DB_PATH = str(config.DATA_DIR / "ftseek.db")

def fix_inconsistency():
    """修复 trades 和 positions 表的不一致"""
    print("=" * 60)
    print("P0 修复：修复数据库不一致问题")
    print("=" * 60)
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    # 1. 检查当前状态
    print("\n📊 当前数据库状态:")
    print("-" * 40)
    
    c.execute("SELECT code, action, quantity, entry_price, status FROM positions WHERE status='OPEN'")
    positions_open = c.fetchall()
    print(f"positions 表 OPEN 状态：{len(positions_open)} 条")
    for p in positions_open:
        print(f"  - {p['code']} {p['action']} {p['quantity']}股 @ {p['entry_price']}")
    
    c.execute("SELECT code, action, quantity, entry_price, status FROM trades WHERE status='open'")
    trades_open = c.fetchall()
    print(f"trades 表 open 状态：{len(trades_open)} 条")
    for t in trades_open:
        print(f"  - {t['code']} {t['action']} {t['quantity']}股 @ {t['entry_price']}")
    
    # 2. 找出差异
    print("\n🔍 分析差异:")
    print("-" * 40)
    
    position_codes = {p['code']: p for p in positions_open}
    trade_codes = {t['code']: t for t in trades_open}
    
    # positions 有但 trades 没有
    pos_only = set(position_codes.keys()) - set(trade_codes.keys())
    # trades 有但 positions 没有
    trade_only = set(trade_codes.keys()) - set(position_codes.keys())
    # 都有的
    both = set(position_codes.keys()) & set(trade_codes.keys())
    
    print(f"仅在 positions 表：{pos_only or '无'}")
    print(f"仅在 trades 表：{trade_only or '无'}")
    print(f"两表都有：{both or '无'}")
    
    # 3. 修复不一致
    print("\n🔧 开始修复:")
    print("-" * 40)
    
    fixed_count = 0
    
    # 修复 1: positions 有但 trades 没有 → 在 trades 表创建记录
    for code in pos_only:
        pos = position_codes[code]
        trade_id = f"fix_trade_{code}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        
        try:
            c.execute("""
                INSERT INTO trades (
                    id, signal_id, code, name, action,
                    entry_price, exit_price, quantity,
                    pnl, pnl_percent, fees,
                    strategy, entry_time, exit_time,
                    exit_reason, order_id, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade_id,
                f"fix_signal_{trade_id}",
                code,
                '',
                pos['action'],
                pos['entry_price'],
                None,
                pos['quantity'],
                0,
                0,
                0,
                'fix_sync',
                datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                None,
                '',
                f'fix_{trade_id}',
                'open'
            ))
            print(f"✅ [trades] 创建记录：{code} {pos['action']} {pos['quantity']}股 @ {pos['entry_price']}")
            fixed_count += 1
        except Exception as e:
            print(f"❌ [trades] 创建失败 {code}: {e}")
    
    # 修复 2: trades 有但 positions 没有 → 在 positions 表创建记录
    for code in trade_only:
        trade = trade_codes[code]
        
        try:
            c.execute("""
                INSERT INTO positions (
                    code, action, quantity, entry_price, entry_time,
                    status, current_price, unrealized_pnl
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                code,
                trade['action'],
                trade['quantity'],
                trade['entry_price'],
                trade['entry_time'],
                'OPEN',
                trade['entry_price'],
                0
            ))
            print(f"✅ [positions] 创建记录：{code} {trade['action']} {trade['quantity']}股 @ {trade['entry_price']}")
            fixed_count += 1
        except Exception as e:
            print(f"❌ [positions] 创建失败 {code}: {e}")
    
    # 修复 3: 两表都有但数据不一致 → 以 positions 为准更新 trades
    for code in both:
        pos = position_codes[code]
        trade = trade_codes[code]
        
        if (pos['quantity'] != trade['quantity'] or 
            abs(pos['entry_price'] - trade['entry_price']) > 0.01):
            
            try:
                c.execute("""
                    UPDATE trades SET
                        quantity = ?,
                        entry_price = ?
                    WHERE code = ? AND status = 'open'
                """, (pos['quantity'], pos['entry_price'], code))
                print(f"✅ [trades] 更新 {code}: qty={trade['quantity']}→{pos['quantity']}, "
                      f"price={trade['entry_price']}→{pos['entry_price']}")
                fixed_count += 1
            except Exception as e:
                print(f"❌ [trades] 更新失败 {code}: {e}")
    
    # 提交事务
    conn.commit()
    conn.close()
    
    # 4. 验证修复结果
    print("\n✅ 修复完成!")
    print("-" * 40)
    print(f"共修复 {fixed_count} 条记录")
    
    # 重新检查状态
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    c.execute("SELECT COUNT(*) as cnt FROM positions WHERE status='OPEN'")
    pos_count = c.fetchone()['cnt']
    
    c.execute("SELECT COUNT(*) as cnt FROM trades WHERE status='open'")
    trade_count = c.fetchone()['cnt']
    
    print(f"\n📊 修复后状态:")
    print(f"  positions 表 OPEN: {pos_count} 条")
    print(f"  trades 表 open: {trade_count} 条")
    
    if pos_count == trade_count:
        print("\n✅ 两表数据已同步!")
    else:
        print(f"\n⚠️  仍有差异：positions={pos_count}, trades={trade_count}")
    
    conn.close()
    
    return fixed_count

if __name__ == '__main__':
    fix_inconsistency()
