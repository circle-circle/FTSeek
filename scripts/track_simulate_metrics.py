#!/usr/bin/env python3.8
"""
模拟盘指标追踪脚本
每日自动统计和记录交易指标
"""
import os
import json
import time
from datetime import datetime, timedelta
from pathlib import Path

# 指标文件路径
METRICS_FILE = Path('/opt/ftseek/FTSeek/data/simulate_metrics.json')

def load_metrics():
    """加载指标"""
    if METRICS_FILE.exists():
        with open(METRICS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {
        "start_date": datetime.now().strftime("%Y-%m-%d"),
        "trading_days": 0,
        "total_trades": 0,
        "winning_trades": 0,
        "losing_trades": 0,
        "total_pnl": 0.0,
        "gross_profit": 0.0,
        "gross_loss": 0.0,
        "max_drawdown": 0.0,
        "daily_pnl": [],
        "trades": []
    }

def save_metrics(metrics):
    """保存指标"""
    METRICS_FILE.parent.mkdir(exist_ok=True)
    with open(METRICS_FILE, 'w', encoding='utf-8') as f:
        json.dump(metrics, f, indent=2, ensure_ascii=False)

def add_trade(code, direction, quantity, entry_price, exit_price, pnl):
    """添加交易记录"""
    metrics = load_metrics()
    
    trade = {
        "code": code,
        "direction": direction,
        "quantity": quantity,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "pnl": pnl,
        "pnl_percent": pnl / (entry_price * quantity),
        "timestamp": time.time()
    }
    
    metrics["trades"].append(trade)
    metrics["total_trades"] += 1
    
    if pnl > 0:
        metrics["winning_trades"] += 1
        metrics["gross_profit"] += pnl
    else:
        metrics["losing_trades"] += 1
        metrics["gross_loss"] += abs(pnl)
    
    metrics["total_pnl"] += pnl
    
    save_metrics(metrics)
    print(f"✅ 交易记录已添加：{code} {direction} {quantity}股 PnL={pnl:.2f}")

def daily_summary():
    """生成每日总结"""
    metrics = load_metrics()
    
    today = datetime.now().strftime("%Y-%m-%d")
    today_trades = [t for t in metrics["trades"] 
                   if datetime.fromtimestamp(t["timestamp"]).strftime("%Y-%m-%d") == today]
    
    if not today_trades:
        print(f"📊 {today} 无交易")
        return
    
    today_pnl = sum(t["pnl"] for t in today_trades)
    today_wins = sum(1 for t in today_trades if t["pnl"] > 0)
    today_losses = len(today_trades) - today_wins
    
    win_rate = today_wins / len(today_trades) if today_trades else 0
    
    print(f"\n{'='*60}")
    print(f"📊 每日总结 - {today}")
    print(f"{'='*60}")
    print(f"交易数：{len(today_trades)}")
    print(f"盈利：{today_wins} 亏损：{today_losses}")
    print(f"胜率：{win_rate:.1%}")
    print(f"盈亏：{today_pnl:+.2f} 港元")
    print(f"{'='*60}\n")
    
    # 添加到每日记录
    metrics["daily_pnl"].append({
        "date": today,
        "trades": len(today_trades),
        "pnl": today_pnl,
        "win_rate": win_rate
    })
    
    # 更新交易天数
    unique_days = set(d["date"] for d in metrics["daily_pnl"])
    metrics["trading_days"] = len(unique_days)
    
    save_metrics(metrics)

def check_readiness():
    """检查是否达到实盘标准"""
    metrics = load_metrics()
    
    print(f"\n{'='*60}")
    print("🎯 实盘切换标准检查")
    print(f"{'='*60}")
    
    # 计算指标
    total_trades = metrics["total_trades"]
    win_rate = metrics["winning_trades"] / total_trades if total_trades > 0 else 0
    profit_factor = metrics["gross_profit"] / metrics["gross_loss"] if metrics["gross_loss"] > 0 else float('inf')
    trading_days = metrics["trading_days"]
    
    # 检查标准
    checks = [
        ("模拟盘天数 ≥ 14", trading_days >= 14, trading_days),
        ("总交易数 ≥ 100", total_trades >= 100, total_trades),
        ("胜率 ≥ 55%", win_rate >= 0.55, f"{win_rate:.1%}"),
        ("盈亏比 ≥ 1.5", profit_factor >= 1.5, f"{profit_factor:.2f}"),
        ("最大回撤 ≤ 15%", metrics["max_drawdown"] <= 0.15, f"{metrics['max_drawdown']:.1%}"),
    ]
    
    passed = 0
    for name, ok, value in checks:
        status = "✅" if ok else "❌"
        print(f"{status} {name}: {value}")
        if ok:
            passed += 1
    
    print(f"\n通过率：{passed}/{len(checks)}")
    
    if passed == len(checks):
        print("\n🎉 所有标准已达成！可以考虑切换到实盘！")
    else:
        print("\n⚠️ 尚未达到实盘标准，请继续模拟盘验证")
    
    print(f"{'='*60}\n")

if __name__ == '__main__':
    import sys
    
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == 'add':
            # 添加交易记录
            code = sys.argv[2]
            direction = sys.argv[3]
            quantity = int(sys.argv[4])
            entry = float(sys.argv[5])
            exit_p = float(sys.argv[6])
            pnl = float(sys.argv[7])
            add_trade(code, direction, quantity, entry, exit_p, pnl)
        elif cmd == 'summary':
            daily_summary()
        elif cmd == 'check':
            check_readiness()
    else:
        print("用法:")
        print("  python3.8 track_simulate_metrics.py add <code> <direction> <qty> <entry> <exit> <pnl>")
        print("  python3.8 track_simulate_metrics.py summary")
        print("  python3.8 track_simulate_metrics.py check")
