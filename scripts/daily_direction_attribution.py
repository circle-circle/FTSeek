#!/usr/bin/env python3
"""
每日LONG/SHORT方向自适应止盈效果分析
基于signal_snapshots.db实际schema，分析方向+策略的前瞻收益表现

输出:
1. LONG vs SHORT 分策略胜率/期望收益
2. LONG分层止盈效果预估（基于fwd_ret分布）
3. 异常检测（胜率/盈亏比跌破阈值）
4. 趋势跟踪（7天滚动）
"""
import sqlite3
import json
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

DB_PATH = Path(__file__).parent.parent / "data" / "signal_snapshots.db"
REPORT_PATH = Path(__file__).parent.parent / "data" / "reports"
REPORT_PATH.mkdir(exist_ok=True)


def analyze_direction(days: int = 7) -> dict:
    """分析最近N天各方向+策略的表现"""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    
    # 获取最近N天有T1回填的信号
    rows = conn.execute(f"""
        SELECT direction, strategy_id, fwd_ret_t1, fwd_ret_t5, strength, confidence,
               symbol, trade_date, created_at_snap
        FROM signal_snapshots
        WHERE fwd_ret_t1 IS NOT NULL
          AND direction != 'NEUTRAL'
          AND trade_date >= date('now', '-{days} days')
        ORDER BY trade_date DESC
    """).fetchall()
    
    if not rows:
        conn.close()
        return {"error": "No data found", "period": f"{days} days", "total": 0}
    
    # 按方向+策略分组
    groups = defaultdict(list)
    for r in rows:
        key = f"{r['direction']}|{r['strategy_id'] or 'unknown'}"
        groups[key].append({
            "fwd_ret_t1": r["fwd_ret_t1"],
            "fwd_ret_t5": r["fwd_ret_t5"],
            "strength": r["strength"],
            "confidence": r["confidence"],
            "symbol": r["symbol"],
            "trade_date": r["trade_date"],
        })
    
    # 计算统计
    results = {}
    for key, signals in groups.items():
        direction, strategy = key.split("|")
        n = len(signals)
        wins_t1 = sum(1 for s in signals if s["fwd_ret_t1"] > 0)
        avg_ret_t1 = sum(s["fwd_ret_t1"] for s in signals) / n * 100
        
        # 盈亏比
        win_rets = [s["fwd_ret_t1"] * 100 for s in signals if s["fwd_ret_t1"] > 0]
        loss_rets = [abs(s["fwd_ret_t1"]) * 100 for s in signals if s["fwd_ret_t1"] <= 0]
        avg_win = sum(win_rets) / len(win_rets) if win_rets else 0
        avg_loss = sum(loss_rets) / len(loss_rets) if loss_rets else 0.001
        pnl_ratio = avg_win / avg_loss if avg_loss > 0 else 99
        
        # 期望值
        wr = wins_t1 / n
        expectancy = wr * avg_win - (1 - wr) * avg_loss
        
        # T5
        t5_signals = [s for s in signals if s["fwd_ret_t5"] is not None]
        if t5_signals:
            avg_ret_t5 = sum(s["fwd_ret_t5"] for s in t5_signals) / len(t5_signals) * 100
            wins_t5 = sum(1 for s in t5_signals if s["fwd_ret_t5"] > 0)
            wr_t5 = wins_t5 / len(t5_signals)
        else:
            avg_ret_t5 = None
            wr_t5 = None
        
        results[key] = {
            "direction": direction,
            "strategy": strategy,
            "n": n,
            "win_rate_t1": wr,
            "avg_ret_t1": avg_ret_t1,
            "pnl_ratio": pnl_ratio,
            "expectancy": expectancy,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "t5": {
                "n": len(t5_signals),
                "win_rate": wr_t5,
                "avg_ret": avg_ret_t5,
            },
        }
    
    # 按方向汇总
    dir_summary = {}
    for direction in ["LONG", "SHORT"]:
        dir_signals = [s for k, sigs in groups.items() 
                       if k.startswith(direction) for s in sigs]
        if not dir_signals:
            continue
        n = len(dir_signals)
        wins = sum(1 for s in dir_signals if s["fwd_ret_t1"] > 0)
        avg_ret = sum(s["fwd_ret_t1"] for s in dir_signals) / n * 100
        
        win_rets = [s["fwd_ret_t1"] * 100 for s in dir_signals if s["fwd_ret_t1"] > 0]
        loss_rets = [abs(s["fwd_ret_t1"]) * 100 for s in dir_signals if s["fwd_ret_t1"] <= 0]
        avg_win = sum(win_rets) / len(win_rets) if win_rets else 0
        avg_loss = sum(loss_rets) / len(loss_rets) if loss_rets else 0.001
        
        dir_summary[direction] = {
            "n": n,
            "win_rate": wins / n,
            "avg_ret_t1": avg_ret,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "pnl_ratio": avg_win / avg_loss if avg_loss > 0 else 99,
            "expectancy": (wins/n) * avg_win - (1 - wins/n) * avg_loss,
        }
    
    conn.close()
    
    return {
        "period": f"{days} days",
        "total": len(rows),
        "by_strategy": results,
        "by_direction": dir_summary,
    }


def format_report(data: dict) -> str:
    """格式化报告"""
    if data.get("error"):
        return f"❌ {data['error']}"
    
    lines = []
    lines.append("📊 LONG/SHORT 方向自适应止盈 — 每日分析报告")
    lines.append(f"分析周期: 最近{data['period']} | 总信号: {data['total']}")
    lines.append("")
    
    # 方向汇总
    lines.append("━━━ 方向汇总 ━━━")
    lines.append(f"{'方向':6s} {'信号数':>6s} {'胜率':>8s} {'T1收益':>10s} {'盈亏比':>8s} {'期望值':>10s}")
    for d in ["LONG", "SHORT"]:
        if d not in data["by_direction"]:
            continue
        s = data["by_direction"][d]
        icon = "📈" if d == "LONG" else "📉"
        lines.append(
            f"{icon}{d:4s} {s['n']:6d} {s['win_rate']:7.1%} "
            f"{s['avg_ret_t1']:+9.3f}% {s['pnl_ratio']:7.2f}x {s['expectancy']:+9.3f}%"
        )
    
    lines.append("")
    
    # 策略细分
    lines.append("━━━ 策略×方向 细分 ━━━")
    sorted_keys = sorted(data["by_strategy"].keys(), key=lambda k: -data["by_strategy"][k]["n"])
    for key in sorted_keys:
        s = data["by_strategy"][key]
        icon = "📈" if s["direction"] == "LONG" else "📉"
        
        # 异常标记
        alert = ""
        if s["win_rate_t1"] < 0.40 and s["n"] >= 10:
            alert = " 🔴"
        elif s["win_rate_t1"] >= 0.58 and s["n"] >= 10:
            alert = " 🟢"
        
        t5_info = ""
        if s["t5"]["n"] >= 10 and s["t5"]["avg_ret"] is not None:
            t5_info = f" T5={s['t5']['avg_ret']:+.2f}%"
        
        lines.append(
            f"  {icon} {s['direction']:5s} {s['strategy']:30s} "
            f"n={s['n']:3d} WR={s['win_rate_t1']:.0%} "
            f"ret={s['avg_ret_t1']:+.3f}% W/L={s['pnl_ratio']:.2f}x{t5_info}{alert}"
        )
    
    lines.append("")
    
    # 异常检测
    anomalies = []
    for key, s in data["by_strategy"].items():
        if s["n"] < 10:
            continue
        
        if s["direction"] == "LONG" and s["win_rate_t1"] < 0.45:
            anomalies.append(
                f"🔴 LONG+{s['strategy']} 胜率={s['win_rate_t1']:.0%} (<45%), n={s['n']}"
            )
        if s["direction"] == "LONG" and s["pnl_ratio"] < 0.85 and s["n"] >= 20:
            anomalies.append(
                f"⚠️ LONG+{s['strategy']} 盈亏比={s['pnl_ratio']:.2f} (<0.85), "
                f"分层止盈可能未生效"
            )
        if s["direction"] == "SHORT" and s["win_rate_t1"] < 0.50 and s["n"] >= 20:
            anomalies.append(
                f"⚠️ SHORT+{s['strategy']} 胜率={s['win_rate_t1']:.0%} (<50%), n={s['n']}"
            )
    
    if anomalies:
        lines.append("━━━ ⚠️ 异常检测 ━━━")
        for a in anomalies:
            lines.append(f"  {a}")
        lines.append("")
    
    # OKR进度
    lines.append("━━━ OKR Phase 1 进度 ━━━")
    long_tech = data["by_strategy"].get("LONG|TECHNICAL_MULTI_FACTOR")
    short_tech = data["by_strategy"].get("SHORT|TECHNICAL_MULTI_FACTOR")
    
    if long_tech:
        pnl_ok = long_tech["pnl_ratio"] >= 1.1
        wr_ok = long_tech["win_rate_t1"] >= 0.48
        lines.append(
            f"  LONG盈亏比 ≥1.1x: {long_tech['pnl_ratio']:.2f}x "
            f"{'✅' if pnl_ok else '❌'}"
        )
        lines.append(
            f"  LONG胜率 ≥48%: {long_tech['win_rate_t1']:.0%} "
            f"{'✅' if wr_ok else '❌'}"
        )
    else:
        lines.append("  LONG数据不足，等待更多信号")
    
    if short_tech:
        wr_ok = short_tech["win_rate_t1"] >= 0.55
        t5_ok = short_tech["t5"]["avg_ret"] is not None and short_tech["t5"]["avg_ret"] >= 2.5
        lines.append(
            f"  SHORT胜率 ≥55%: {short_tech['win_rate_t1']:.0%} "
            f"{'✅' if wr_ok else '❌'}"
        )
        if short_tech["t5"]["avg_ret"] is not None:
            lines.append(
                f"  SHORT T5 ≥2.5%: {short_tech['t5']['avg_ret']:+.2f}% "
                f"{'✅' if t5_ok else '❌'}"
            )
    
    return "\n".join(lines)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="每日方向自适应分析")
    parser.add_argument("--days", type=int, default=7, help="分析最近N天 (默认7天)")
    parser.add_argument("--output", type=str, help="输出文件路径")
    parser.add_argument("--json", action="store_true", help="同时输出JSON")
    args = parser.parse_args()
    
    data = analyze_direction(args.days)
    report = format_report(data)
    
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"报告已保存到: {output_path}")
    
    print(report)
    
    # 保存JSON
    if args.json:
        json_path = REPORT_PATH / f"direction_attribution_{datetime.now().strftime('%Y%m%d')}.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump({
                "timestamp": datetime.now().isoformat(),
                "period_days": args.days,
                "data": {k: v for k, v in data.items() if k != "by_strategy"},
                "by_strategy": {k: {kk: vv for kk, vv in v.items()} 
                                for k, v in data.get("by_strategy", {}).items()},
            }, f, indent=2, ensure_ascii=False, default=str)
        print(f"\nJSON已保存: {json_path}")


if __name__ == "__main__":
    main()
