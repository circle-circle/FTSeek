#!/usr/bin/env python3
"""
FTSeek-US 持仓同步工具
对比数据库持仓与富途实际持仓，发现不一致时告警
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlite3
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "data" / "ftseek_us.db"

def get_db_positions() -> dict:
    """获取数据库持仓"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT symbol, direction, size, avg_entry_price, current_sl
        FROM open_positions WHERE size > 0
    ''')
    rows = cursor.fetchall()
    conn.close()
    
    positions = {}
    for symbol, direction, size, entry, sl in rows:
        positions[symbol] = {
            "direction": direction,
            "size": float(size) if size else 0,
            "entry": float(entry) if entry else 0,
            "sl": float(sl) if sl else 0,
        }
    return positions

def get_futu_positions() -> dict:
    """获取富途模拟账户持仓"""
    positions = {}
    try:
        from futu import OpenSecTradeContext, TrdMarket, TrdEnv, RET_OK
        
        trd_ctx = OpenSecTradeContext(
            filter_trdmarket=TrdMarket.US,
            host='127.0.0.1',
            port=11111,
            is_encrypt=False
        )
        
        ret, data = trd_ctx.position_list_query(trd_env=TrdEnv.SIMULATE)
        trd_ctx.close()
        
        if ret == RET_OK:
            for idx, pos in data.iterrows():
                code = pos.get('code', '')
                qty = pos.get('qty', 0)
                cost = pos.get('cost_price', 0)
                
                # qty < 0 表示空头持仓
                if qty != 0:
                    positions[code] = {
                        "direction": "SHORT" if qty < 0 else "LONG",
                        "size": abs(qty),
                        "entry": float(cost) if cost else 0,
                    }
    except Exception as e:
        logger.error(f"获取富途持仓失败: {e}")
    
    return positions

def compare_positions(db_pos: dict, futu_pos: dict) -> dict:
    """对比持仓差异"""
    all_symbols = set(db_pos.keys()) | set(futu_pos.keys())
    
    diff = {
        "match": [],
        "mismatch": [],
        "db_only": [],
        "futu_only": [],
    }
    
    for symbol in all_symbols:
        db = db_pos.get(symbol)
        futu = futu_pos.get(symbol)
        
        # 转换 symbol 格式 (US.TSLA -> US.TSLA)
        futu_symbol = symbol.replace("US.", "") if symbol.startswith("US.") else symbol
        
        if db and futu:
            # 两边都有
            if db["size"] == futu["size"] and db["direction"] == futu["direction"]:
                diff["match"].append(symbol)
            else:
                diff["mismatch"].append({
                    "symbol": symbol,
                    "db": db,
                    "futu": futu,
                })
        elif db and not futu:
            diff["db_only"].append({"symbol": symbol, **db})
        else:
            diff["futu_only"].append({"symbol": symbol, **futu})
    
    return diff

def sync_positions(dry_run: bool = True) -> dict:
    """同步持仓"""
    db_pos = get_db_positions()
    futu_pos = get_futu_positions()
    diff = compare_positions(db_pos, futu_pos)
    
    result = {
        "timestamp": datetime.now().isoformat(),
        "db_positions": len(db_pos),
        "futu_positions": len(futu_pos),
        "diff": diff,
        "action": "dry_run" if dry_run else "synced",
    }
    
    if not dry_run:
        # 清理数据库中不存在的持仓
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        for item in diff["db_only"]:
            if item["size"] == 0:
                cursor.execute('DELETE FROM open_positions WHERE symbol = ?', (item["symbol"],))
                logger.info(f"[同步] 删除空持仓: {item['symbol']}")
        
        conn.commit()
        conn.close()
    
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
        print("FTSeek-US 持仓同步检查")
        print("=" * 60)
        print(f"时间: {result['timestamp']}")
        print(f"数据库持仓: {result['db_positions']} 个")
        print(f"富途持仓: {result['futu_positions']} 个")
        print()
        
        if result['diff']['match']:
            print(f"✅ 一致: {len(result['diff']['match'])} 个")
        
        if result['diff']['mismatch']:
            print(f"⚠️ 不一致: {len(result['diff']['mismatch'])} 个")
            for item in result['diff']['mismatch']:
                print(f"   {item['symbol']}: DB={item['db']} vs Futu={item['futu']}")
        
        if result['diff']['db_only']:
            print(f"🔴 仅数据库: {len(result['diff']['db_only'])} 个")
            for item in result['diff']['db_only']:
                print(f"   {item['symbol']}: {item['direction']} x{item['size']}")
        
        if result['diff']['futu_only']:
            print(f"🔵 仅富途: {len(result['diff']['futu_only'])} 个")
            for item in result['diff']['futu_only']:
                print(f"   {item['symbol']}: {item['direction']} x{item['size']}")
        
        print("=" * 60)
        
        if not args.sync and (result['diff']['mismatch'] or result['diff']['db_only']):
            print("提示: 使用 --sync 执行同步")
