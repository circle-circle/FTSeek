#!/usr/bin/env python3
"""
FTSeek 港股持仓同步脚本 V2
使用配置的模拟账户ID查询持仓
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlite3
import logging
from datetime import datetime
from pathlib import Path
import yaml

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("ftseek.position_sync")

DB_PATH = Path(__file__).parent.parent / "data" / "ftseek.db"
CONFIG_PATH = Path(__file__).parent.parent / "config" / "app_config.yaml"

def get_simulate_account_id() -> str:
    """从配置文件获取模拟账户ID"""
    try:
        with open(CONFIG_PATH, 'r') as f:
            config = yaml.safe_load(f)
        return str(config.get('futu', {}).get('simulate_account_id', '8246818'))
    except:
        return '8246818'

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

def get_futu_positions(account_id: str, max_retries: int = 5) -> dict:
    """
    获取富途指定模拟账户的持仓
    
    2026-03-27 修复: 增加重试逻辑，等待 OpenD 数据就绪
    """
    import time
    positions = {}
    retry_interval = 10  # 每次等待 10 秒
    
    for attempt in range(max_retries):
        try:
            from futu import OpenSecTradeContext, TrdMarket, TrdEnv, RET_OK
            
            trd_ctx = OpenSecTradeContext(
                filter_trdmarket=TrdMarket.HK,
                host='127.0.0.1',
                port=11111
            )
            
            ret, data = trd_ctx.position_list_query(
                trd_env=TrdEnv.SIMULATE,
                acc_id=int(account_id)
            )
            trd_ctx.close()
            
            if ret == RET_OK:
                for idx, pos in data.iterrows():
                    code = pos.get('code', '')
                    qty = pos.get('qty', 0)
                    cost = pos.get('cost_price', 0)
                    
                    # 只处理港股正股 (qty > 0)
                    if code.startswith('HK.') and len(code.split('.')[-1]) == 5 and qty > 0:
                        positions[code] = {
                            "qty": int(qty),
                            "cost": float(cost) if cost else 0,
                        }
                logger.info(f"成功获取富途持仓: {len(positions)} 个")
                return positions
            
            # 检查是否需要重试
            error_msg = str(data)
            if "暂时还未准备好" in error_msg or "not ready" in error_msg.lower():
                if attempt < max_retries - 1:
                    logger.warning(f"[重试 {attempt+1}/{max_retries}] OpenD 数据未就绪，等待 {retry_interval} 秒")
                    time.sleep(retry_interval)
                    continue
                else:
                    logger.error(f"达到最大重试次数，获取富途持仓失败: {data}")
            else:
                logger.error(f"获取富途持仓失败: {data}")
                return positions
                
        except Exception as e:
            if attempt < max_retries - 1:
                logger.warning(f"[重试 {attempt+1}/{max_retries}] 异常: {e}，等待重试")
                time.sleep(retry_interval)
                continue
            logger.error(f"获取富途持仓失败: {e}")
    
    return positions

def sync_positions(dry_run: bool = True) -> dict:
    """同步持仓"""
    account_id = get_simulate_account_id()
    logger.info(f"使用模拟账户ID: {account_id}")
    
    db_pos = get_db_positions()
    futu_pos = get_futu_positions(account_id)
    
    db_codes = set(db_pos.keys())
    futu_codes = set(futu_pos.keys())
    
    result = {
        "timestamp": datetime.now().isoformat(),
        "account_id": account_id,
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
    parser.add_argument("--sync", action="store_true", help="执行同步")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args()
    
    result = sync_positions(dry_run=not args.sync)
    
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    else:
        print("=" * 60)
        print("FTSeek 港股持仓同步 V2")
        print("=" * 60)
        print(f"时间: {result['timestamp']}")
        print(f"模拟账户ID: {result['account_id']}")
        print(f"\n数据库持仓: {len(result['db_positions'])} 个")
        print(f"富途持仓: {len(result['futu_positions'])} 个")
        
        if result['matched']:
            print(f"\n✅ 匹配: {result['matched']}")
        
        if result['in_db_only']:
            print(f"\n🔴 仅数据库 (需关闭): {result['in_db_only']}")
        
        if result['in_futu_only']:
            print(f"\n🔵 仅富途 (需添加): {result['in_futu_only']}")
        
        if not result['in_db_only'] and not result['in_futu_only']:
            print("\n✅ 持仓已同步")
        
        if result['synced']:
            print("\n✅ 同步完成")
        
        print("=" * 60)
