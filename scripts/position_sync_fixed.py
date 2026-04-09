#!/usr/bin/env python3
"""
FTSeek 港股持仓同步脚本 (修复版)
正确对比数据库与富途持仓
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlite3
import logging
from datetime import datetime
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("ftseek.position_sync")

DB_PATH = Path(__file__).parent.parent / "data" / "ftseek.db"

def get_db_positions() -> dict:
    """获取数据库持仓"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT code, quantity, entry_price, stop_loss, take_profit
        FROM positions WHERE status = 'OPEN'
    ''')
    rows = cursor.fetchall()
    conn.close()
    
    positions = {}
    for code, qty, entry, sl, tp in rows:
        positions[code] = {
            "qty": int(qty) if qty else 0,
            "entry": float(entry) if entry else 0,
            "sl": float(sl) if sl else 0,
            "tp": float(tp) if tp else 0,
        }
    return positions

def get_futu_positions() -> dict:
    """获取富途港股持仓"""
    positions = {}
    try:
        from futu import OpenSecTradeContext, TrdMarket, RET_OK
        
        trd_ctx = OpenSecTradeContext(
            filter_trdmarket=TrdMarket.HK,
            host='127.0.0.1',
            port=11111
        )
        
        ret, data = trd_ctx.position_list_query()
        trd_ctx.close()
        
        if ret == RET_OK:
            for idx, pos in data.iterrows():
                code = pos.get('code', '')
                qty = pos.get('qty', 0)
                cost = pos.get('cost_price', 0)
                
                # 只处理港股正股 (排除期权等)
                if code.startswith('HK.') and len(code.split('.')[-1]) == 5:
                    positions[code] = {
                        "qty": int(qty) if qty else 0,
                        "cost": float(cost) if cost else 0,
                    }
    except Exception as e:
        logger.error(f"获取富途持仓失败: {e}")
    
    return positions

def sync_positions(dry_run: bool = True) -> dict:
    """同步持仓"""
    db_pos = get_db_positions()
    futu_pos = get_futu_positions()
    
    db_codes = set(db_pos.keys())
    futu_codes = set(futu_pos.keys())
    
    result = {
        "timestamp": datetime.now().isoformat(),
        "db_positions": db_pos,
        "futu_positions": futu_pos,
        "in_db_only": list(db_codes - futu_codes),
        "in_futu_only": list(futu_codes - db_codes),
        "matched": list(db_codes & futu_codes),
        "synced": False,
    }
    
    if not dry_run:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # 关闭数据库中不存在于富途的持仓
        for code in result["in_db_only"]:
            logger.info(f"[同步] 关闭无效持仓: {code}")
            cursor.execute('''
                UPDATE positions 
                SET status = 'CLOSED', 
                    exit_time = ?,
                    exit_price = entry_price,
                    realized_pnl = 0
                WHERE code = ? AND status = 'OPEN'
            ''', (datetime.now().isoformat(), code))
        
        # 添加富途中不存在于数据库的持仓
        for code in result["in_futu_only"]:
            pos = futu_pos[code]
            logger.info(f"[同步] 添加持仓: {code} x{pos['qty']} @ {pos['cost']}")
            cursor.execute('''
                INSERT INTO positions (
                    code, action, quantity, entry_price, entry_time,
                    status, stop_loss, take_profit
                ) VALUES (?, 'BUY', ?, ?, ?, 'OPEN', ?, ?)
            ''', (
                code,
                pos["qty"],
                pos["cost"],
                datetime.now().isoformat(),
                round(pos["cost"] * 0.97, 2),
                round(pos["cost"] * 1.05, 2),
            ))
        
        conn.commit()
        conn.close()
        result["synced"] = True
    
    return result

if __name__ == "__main__":
    import argparse
    import json
    
    parser = argparse.ArgumentParser()
    parser.add_argument("--sync", action="store_true", help="执行同步 (默认只检查)")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args()
    
    result = sync_positions(dry_run=not args.sync)
    
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    else:
        print("=" * 60)
        print("FTSeek 港股持仓同步")
        print("=" * 60)
        print(f"时间: {result['timestamp']}")
        print(f"\n数据库持仓: {len(result['db_positions'])} 个")
        print(f"富途持仓: {len(result['futu_positions'])} 个")
        
        if result['matched']:
            print(f"\n✅ 匹配: {result['matched']}")
        
        if result['in_db_only']:
            print(f"\n🔴 仅数据库 (需关闭): {result['in_db_only']}")
        
        if result['in_futu_only']:
            print(f"\n🔵 仅富途 (需添加): {result['in_futu_only']}")
        
        if not args.sync and (result['in_db_only'] or result['in_futu_only']):
            print("\n提示: 使用 --sync 执行同步")
        
        if result['synced']:
            print("\n✅ 同步完成")
        
        print("=" * 60)
