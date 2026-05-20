#!/usr/bin/env python3
"""
期权持仓同步脚本
查询 Futu 实际期权持仓，同步到数据库
"""
import sys
import sqlite3
from datetime import datetime

sys.path.insert(0, "/opt/ftseek/FTSeek-US")

from app.execution.broker_adapter import FutuBrokerAdapter as BrokerAdapter
from app.core.config_container import get_config

def sync_option_positions():
    """同步 Futu 期权持仓到数据库"""
    # 初始化 broker adapter
    cfg = get_config()
    futu_cfg = cfg.get_section("futu", {})
    
    adapter = BrokerAdapter(
        host=futu_cfg.get("host", "127.0.0.1"),
        port=futu_cfg.get("port", 11111),
        trade_env=futu_cfg.get("trade_env", 1),  # 1=模拟盘
        account_id=futu_cfg.get("account_id", ""),
    )
    
    if not adapter.connect():
        print("❌ 连接 Futu 失败")
        return False
    
    print("✅ 连接 Futu 成功")
    
    # 查询期权持仓
    opt_positions = adapter.get_option_positions()
    print(f"\n=== Futu 期权持仓 ===")
    if not opt_positions:
        print("无期权持仓")
        adapter.disconnect()
        return True
    
    for pos in opt_positions:
        print(f"  {pos['option_code']} | qty={pos['qty']} | cost={pos['cost_price']:.2f} | pnl={pos['unrealized_pnl']:.2f}")
    
    # 同步到数据库
    db_path = "/opt/ftseek/FTSeek-US/data/ftseek_us.db"
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    now = datetime.now().isoformat()
    synced = 0
    
    for pos in opt_positions:
        option_code = pos["option_code"]
        symbol = option_code.split(".")[1][:4] if "." in option_code else option_code[:4]
        
        # 检查是否已存在
        cursor.execute("SELECT option_code FROM option_positions WHERE option_code = ?", (option_code,))
        if cursor.fetchone():
            print(f"⚠️  {option_code} 已存在，跳过")
            continue
        
        # 插入新持仓
        cursor.execute("""
            INSERT INTO option_positions (
                symbol, option_code, direction, qty, entry_price,
                strike_price, expiry_date, entry_time, cost_total,
                signal_strength, signal_confidence, is_simulated, mode, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            symbol,
            option_code,
            "LONG_PUT",  # 买入 Put
            pos["qty"],
            pos["cost_price"],
            None,  # strike_price
            None,  # expiry_date
            now,
            pos["cost_price"] * pos["qty"] * 100,  # cost_total
            0,  # signal_strength
            0.0,  # signal_confidence
            1,  # is_simulated
            "normal",  # mode
            now,
        ))
        synced += 1
        print(f"✅ 同步 {option_code} 到数据库")
    
    conn.commit()
    conn.close()
    adapter.disconnect()
    
    print(f"\n=== 同步完成：{synced} 笔 ===")
    return True

if __name__ == "__main__":
    sync_option_positions()
