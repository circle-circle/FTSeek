#!/usr/bin/env python3
"""
跨市场持仓报告脚本 (Cross-Market Position Report)
只读聚合 FTSeek (港股) + FTSeek-US (美股) 的持仓数据，输出统一报告。

用途：
- 每日归因统计
- 双市场持仓概览
- PnL 合并分析

不修改任何数据，纯只读。
"""

import sys
import sqlite3
import json
import logging
from datetime import datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("cross_market_report")

# DB 路径
FTSEEK_DB = '/opt/ftseek/FTSeek/data/ftseek.db'
FTSEEK_US_DB = '/opt/ftseek/FTSeek-US/data/ftseek_us.db'


def read_hk_positions():
    """读取 FTSeek 港股持仓 (只读)"""
    if not Path(FTSEEK_DB).exists():
        logger.warning(f"港股 DB 不存在: {FTSEEK_DB}")
        return []
    
    conn = sqlite3.connect(FTSEEK_DB)
    cursor = conn.cursor()
    
    try:
        # 从 positions 表读取 OPEN 持仓 (仅 HK.*)
        cursor.execute("""
            SELECT code, action, quantity, entry_price, entry_time, 
                   stop_loss, take_profit, strategy, trader_type
            FROM positions 
            WHERE status='OPEN' AND code LIKE 'HK.%'
            ORDER BY code
        """)
        
        positions = []
        for row in cursor.fetchall():
            positions.append({
                'code': row[0],
                'action': row[1],
                'quantity': row[2],
                'entry_price': row[3],
                'entry_time': row[4],
                'stop_loss': row[5],
                'take_profit': row[6],
                'strategy': row[7] or 'unknown',
                'trader_type': row[8] or 'MANUAL',
                'market': 'HK'
            })
        
        return positions
    finally:
        conn.close()


def read_us_positions():
    """读取 FTSeek-US 美股持仓 (只读)"""
    if not Path(FTSEEK_US_DB).exists():
        logger.warning(f"美股 DB 不存在: {FTSEEK_US_DB}")
        return []
    
    conn = sqlite3.connect(FTSEEK_US_DB)
    cursor = conn.cursor()
    
    try:
        # 从 open_positions 表读取
        cursor.execute("""
            SELECT symbol, direction, size, avg_entry_price, entry_time,
                   initial_sl, initial_tp, strategy_id, trader_type
            FROM open_positions 
            WHERE sync_status='OPEN'
            ORDER BY symbol
        """)
        
        positions = []
        for row in cursor.fetchall():
            positions.append({
                'code': row[0],
                'action': 'SELL' if row[1] == 'SHORT' else 'BUY',
                'quantity': row[2] if row[1] == 'LONG' else -row[2],
                'entry_price': row[3],
                'entry_time': row[4],
                'stop_loss': row[5],
                'take_profit': row[6],
                'strategy': row[7] or 'unknown',
                'trader_type': row[8] or 'MANUAL',
                'market': 'US'
            })
        
        return positions
    finally:
        conn.close()


def generate_report():
    """生成跨市场持仓报告"""
    logger.info("📊 开始生成跨市场持仓报告...")
    
    hk_positions = read_hk_positions()
    us_positions = read_us_positions()
    all_positions = hk_positions + us_positions
    
    # 统计
    report = {
        'generated_at': datetime.now().isoformat(),
        'summary': {
            'total_positions': len(all_positions),
            'hk_count': len(hk_positions),
            'us_count': len(us_positions),
            'long_count': sum(1 for p in all_positions if p['quantity'] > 0),
            'short_count': sum(1 for p in all_positions if p['quantity'] < 0),
            'ai_count': sum(1 for p in all_positions if p['trader_type'] == 'AI'),
            'manual_count': sum(1 for p in all_positions if p['trader_type'] == 'MANUAL'),
        },
        'hk_positions': hk_positions,
        'us_positions': us_positions,
        'all_positions': all_positions
    }
    
    # 打印报告
    print("\n" + "=" * 70)
    print("📊 跨市场持仓报告 (Cross-Market Position Report)")
    print("=" * 70)
    print(f"生成时间: {report['generated_at']}")
    print(f"\n持仓总计: {report['summary']['total_positions']} 只")
    print(f"  🇭🇰 港股: {report['summary']['hk_count']} 只")
    print(f"  🇺🇸 美股: {report['summary']['us_count']} 只")
    print(f"  多头: {report['summary']['long_count']} | 空头: {report['summary']['short_count']}")
    print(f"  AI: {report['summary']['ai_count']} | Manual: {report['summary']['manual_count']}")
    
    if hk_positions:
        print(f"\n🇭🇰 港股持仓 ({len(hk_positions)} 只):")
        for p in hk_positions:
            print(f"  {p['code']}: {p['action']} {p['quantity']} 股 @ {p['entry_price']} "
                  f"[{p['strategy']}] ({p['trader_type']})")
    
    if us_positions:
        print(f"\n🇺🇸 美股持仓 ({len(us_positions)} 只):")
        for p in us_positions:
            direction = 'SHORT' if p['quantity'] < 0 else 'LONG'
            print(f"  {p['code']}: {direction} {abs(p['quantity'])} 股 @ {p['entry_price']} "
                  f"[{p['strategy']}] ({p['trader_type']})")
    
    # 保存 JSON 报告
    output_path = Path('/opt/ftseek/FTSeek/logs/audit/cross_market_report.json')
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n💾 报告已保存: {output_path}")
    
    return report


if __name__ == '__main__':
    generate_report()
