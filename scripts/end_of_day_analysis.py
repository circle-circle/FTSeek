#!/usr/bin/env python3
"""
收盘后自动分析脚本
- 在美东时间 16:30 自动运行
- 生成完整的日度报告
- 对比 RFC-055 目标
"""
import sqlite3
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

DB = "data/ftseek_us.db"
REPORT_DIR = Path("data/reports")
REPORT_DIR.mkdir(exist_ok=True)

ET = timezone(timedelta(hours=-4))

def generate_report():
    conn = sqlite3.connect(DB)
    cursor = conn.cursor()
    
    now_et = datetime.now(ET)
    today = now_et.strftime('%Y-%m-%d')
    
    # 今日交易
    cursor.execute("""
        SELECT 
            COUNT(*) as trades,
            SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN realized_pnl < 0 THEN 1 ELSE 0 END) as losses,
            ROUND(100.0 * SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) / COUNT(*), 1) as win_rate,
            ROUND(SUM(realized_pnl), 2) as total_pnl,
            ROUND(AVG(realized_pnl), 2) as avg_pnl,
            ROUND(AVG(CASE WHEN realized_pnl > 0 THEN realized_pnl END), 2) as avg_win,
            ROUND(AVG(CASE WHEN realized_pnl < 0 THEN realized_pnl END), 2) as avg_loss,
            GROUP_CONCAT(DISTINCT symbol) as symbols,
            GROUP_CONCAT(DISTINCT direction) as directions,
            GROUP_CONCAT(DISTINCT exit_reason) as exits,
            GROUP_CONCAT(DISTINCT market_regime) as regimes
        FROM trade_records
        WHERE date(entry_time, 'unixepoch', 'localtime') = ?
          AND realized_pnl IS NOT NULL
    """, (today,))
    
    row = cursor.fetchone()
    
    if not row or row[0] == 0:
        print(f"[{today}] 今日无交易")
        conn.close()
        return
    
    trades, wins, losses, wr, total_pnl, avg_pnl, avg_win, avg_loss, symbols, directions, exits, regimes = row
    
    # 生成报告
    report = {
        'date': today,
        'generated_at': now_et.isoformat(),
        'summary': {
            'trades': trades,
            'wins': wins,
            'losses': losses,
            'win_rate': wr,
            'total_pnl': total_pnl,
            'avg_pnl': avg_pnl,
            'avg_win': avg_win,
            'avg_loss': avg_loss,
            'profit_factor': round(abs(avg_win / avg_loss), 2) if avg_loss and avg_loss < 0 else None
        },
        'details': {
            'symbols': symbols.split(',') if symbols else [],
            'directions': directions.split(',') if directions else [],
            'exits': exits.split(',') if exits else [],
            'regimes': regimes.split(',') if regimes else []
        },
        'rfc055_targets': {
            'win_rate_target': 50.0,
            'win_rate_achieved': wr >= 50.0,
            'daily_pnl_target': 500.0,
            'daily_pnl_achieved': total_pnl >= 500.0
        }
    }
    
    # 保存报告
    report_file = REPORT_DIR / f"daily_{today}.json"
    with open(report_file, 'w') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    
    # 打印报告
    print(f"\n{'='*70}")
    print(f"📊 FTSeek-US 日度报告 - {today}")
    print(f"{'='*70}")
    print(f"交易数: {trades} ({wins}胜 / {losses}负)")
    print(f"胜率: {wr}% {'✅' if wr >= 50 else '❌'} (目标: ≥50%)")
    print(f"总PnL: ${total_pnl:.2f} {'✅' if total_pnl >= 500 else '❌'} (目标: ≥$500)")
    print(f"均PnL: ${avg_pnl:.2f}")
    print(f"均盈利: ${avg_win:.2f} | 均亏损: ${avg_loss:.2f}")
    if report['summary']['profit_factor']:
        print(f"盈亏比: {report['summary']['profit_factor']:.2f}")
    print(f"标的: {symbols}")
    print(f"方向: {directions}")
    print(f"退出: {exits}")
    print(f"市况: {regimes}")
    print(f"{'='*70}")
    
    # 检查是否达标
    if wr >= 50 and total_pnl >= 500:
        print("🎯 日度目标达成!")
    elif wr >= 50:
        print("⚠️ 胜率达标，但盈利不足")
    elif total_pnl >= 500:
        print("⚠️ 盈利达标，但胜率不足")
    else:
        print("❌ 日度目标未达成")
    
    print(f"\n报告已保存: {report_file}")
    print(f"{'='*70}\n")
    
    conn.close()

if __name__ == "__main__":
    generate_report()
