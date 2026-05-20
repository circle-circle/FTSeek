#!/usr/bin/env python3
"""
VIX 高波动风控参数自动调整器
当 VIX > 25 时自动收紧止损
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlite3
import json
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "ftseek_us.db"
CONFIG_PATH = Path(__file__).parent.parent / "config" / "strategy_params.json"

# VIX 阈值配置
VIX_THRESHOLDS = {
    "normal": 20,      # VIX < 20: 正常波动
    "elevated": 25,    # 20 <= VIX < 25: 升高波动
    "high": 30,         # VIX >= 25: 高波动
    "extreme": 35,      # VIX >= 35: 极端波动
}

# 止损参数调整
STOP_LOSS_ADJUSTMENTS = {
    "normal": 0.08,     # 正常: 8%
    "elevated": 0.06,   # 升高: 6%
    "high": 0.04,       # 高波动: 4%
    "extreme": 0.03,    # 极端: 3%
}

def get_vix_level(vix_value: float) -> str:
    """获取 VIX 等级"""
    if vix_value < VIX_THRESHOLDS["normal"]:
        return "normal"
    elif vix_value < VIX_THRESHOLDS["elevated"]:
        return "elevated"
    elif vix_value < VIX_THRESHOLDS["high"]:
        return "high"
    else:
        return "extreme"

def adjust_stop_losses(vix_value: float, dry_run: bool = True) -> dict:
    """根据 VIX 调整止损"""
    level = get_vix_level(vix_value)
    new_sl_pct = STOP_LOSS_ADJUSTMENTS[level]
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT symbol, direction, avg_entry_price, current_sl, size
        FROM open_positions
    ''')
    positions = cursor.fetchall()
    
    results = {
        "vix": vix_value,
        "level": level,
        "new_stop_loss_pct": new_sl_pct,
        "positions_checked": len(positions),
        "positions_updated": 0,
        "details": []
    }
    
    for symbol, direction, entry, old_sl, size in positions:
        entry = float(entry)
        old_sl = float(old_sl) if old_sl else 0
        
        if direction == "SHORT":
            new_sl = entry * (1 + new_sl_pct)
        else:
            new_sl = entry * (1 - new_sl_pct)
        
        detail = {
            "symbol": symbol,
            "direction": direction,
            "entry": entry,
            "old_sl": old_sl,
            "new_sl": round(new_sl, 2),
            "changed": abs(new_sl - old_sl) / entry > 0.005 if old_sl else True
        }
        results["details"].append(detail)
        
        if detail["changed"] and not dry_run:
            cursor.execute('''
                UPDATE open_positions 
                SET current_sl = ?, updated_at = ?
                WHERE symbol = ?
            ''', (new_sl, datetime.now().isoformat(), symbol))
            results["positions_updated"] += 1
    
    if not dry_run:
        conn.commit()
    conn.close()
    
    return results

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--vix", type=float, required=True, help="Current VIX value")
    parser.add_argument("--apply", action="store_true", help="Apply changes (default: dry run)")
    args = parser.parse_args()
    
    result = adjust_stop_losses(args.vix, dry_run=not args.apply)
    
    print(f"VIX: {result['vix']} (Level: {result['level']})")
    print(f"New Stop Loss: {result['new_stop_loss_pct']*100}%")
    print(f"Positions checked: {result['positions_checked']}")
    print(f"Positions to update: {result['positions_updated']}")
    
    if not args.apply:
        print("\n[DRY RUN] Use --apply to make changes")
    
    for d in result["details"]:
        status = "⚠️ UPDATE" if d["changed"] else "✅ OK"
        print(f"  {d['symbol']}: {d['old_sl']:.2f} → {d['new_sl']:.2f} {status}")
