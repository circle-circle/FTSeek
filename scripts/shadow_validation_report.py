#!/usr/bin/env python3
"""
Shadow 验证日报 — 每日 ET 18:00 生成

追踪 shadow 模式下的完整链路表现，为切 live 提供决策依据。
"""

import sqlite3
import json
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

DB_PATH = Path("/opt/ftseek/FTSeek-US/data/shadow_logs.db")
REPORT_DIR = Path("/opt/ftseek/FTSeek-US/data/reports")
REPORT_DIR.mkdir(parents=True, exist_ok=True)


def get_shadow_signals(days=7):
    """获取最近 N 天的 shadow 信号"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # 检查表是否存在
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='shadow_signals'")
    if not cursor.fetchone():
        conn.close()
        return []
    
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    cursor.execute("""
        SELECT timestamp, symbol, direction, strength, confidence, 
               strategy_id, regime, market_sentiment, vix
        FROM shadow_signals 
        WHERE timestamp > ?
        ORDER BY timestamp DESC
    """, (cutoff,))
    
    rows = cursor.fetchall()
    conn.close()
    
    return [
        {
            "timestamp": r[0],
            "symbol": r[1],
            "direction": r[2],
            "strength": r[3],
            "confidence": r[4],
            "strategy_id": r[5],
            "regime": r[6],
            "market_sentiment": r[7],
            "vix": r[8],
        }
        for r in rows
    ]


def get_playbook_instructions(days=7):
    """获取最近 N 天的 playbook 指令"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='playbook_instructions'")
    if not cursor.fetchone():
        conn.close()
        return []
    
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    cursor.execute("""
        SELECT timestamp, symbol, direction, entry_price, target_price,
               stop_loss, confidence, status, pnl_pct
        FROM playbook_instructions 
        WHERE timestamp > ?
        ORDER BY timestamp DESC
    """, (cutoff,))
    
    rows = cursor.fetchall()
    conn.close()
    
    return [
        {
            "timestamp": r[0],
            "symbol": r[1],
            "direction": r[2],
            "entry_price": r[3],
            "target_price": r[4],
            "stop_loss": r[5],
            "confidence": r[6],
            "status": r[7],
            "pnl_pct": r[8],
        }
        for r in rows
    ]


def generate_report():
    """生成 shadow 验证日报"""
    today = datetime.now().strftime("%Y-%m-%d")
    report_file = REPORT_DIR / f"shadow_validation_{today}.json"
    
    # 收集数据
    signals = get_shadow_signals(days=7)
    instructions = get_playbook_instructions(days=7)
    
    # 统计
    signal_count = len(signals)
    instruction_count = len(instructions)
    
    # 按方向统计
    direction_stats = defaultdict(lambda: {"count": 0, "pnl": 0.0})
    for inst in instructions:
        d = inst["direction"]
        direction_stats[d]["count"] += 1
        if inst["pnl_pct"] is not None:
            direction_stats[d]["pnl"] += inst["pnl_pct"]
    
    # 按策略统计
    strategy_stats = defaultdict(int)
    for sig in signals:
        strategy_stats[sig["strategy_id"]] += 1
    
    # 按 regime 统计
    regime_stats = defaultdict(int)
    for sig in signals:
        regime_stats[sig["regime"]] += 1
    
    # 生成报告
    report = {
        "date": today,
        "period": "7 days",
        "summary": {
            "total_signals": signal_count,
            "total_instructions": instruction_count,
            "avg_signals_per_day": round(signal_count / 7, 2),
            "avg_instructions_per_day": round(instruction_count / 7, 2),
        },
        "by_direction": {d: {"count": s["count"], "total_pnl_pct": round(s["pnl"], 2)} for d, s in direction_stats.items()},
        "by_strategy": dict(strategy_stats),
        "by_regime": dict(regime_stats),
        "recommendation": "",
    }
    
    # 生成建议
    if signal_count < 10:
        report["recommendation"] = "⚠️ 信号太少（<10），建议延长验证期或检查 InsiderScanLoop"
    elif instruction_count < 5:
        report["recommendation"] = "⚠️ 指令太少（<5），建议检查 Playbook 生成逻辑"
    elif direction_stats["LONG"]["count"] < 3:
        report["recommendation"] = "⚠️ LONG 信号太少，动态惩罚可能仍过于严格"
    else:
        report["recommendation"] = "✅ Shadow 验证数据充足，可以考虑切 live"
    
    # 保存
    with open(report_file, "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    
    print(f"📊 Shadow 验证日报已生成: {report_file}")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    
    return report


if __name__ == "__main__":
    generate_report()
