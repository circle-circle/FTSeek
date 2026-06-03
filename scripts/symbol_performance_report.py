#!/usr/bin/env python3
"""
ADR-004 (2026-06-04): 标的表现周报 — 自动生成 + 飞书推送

分析最近 14 天 MAIN_PIPELINE 交易数据，按标的聚合统计：
- 总交易数、胜率、累计盈亏、平均盈亏
- 标注正 EV / 负 EV / 样本不足
- 建议禁用/保留/观察

数据驱动，不靠人猜。
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(PROJECT_DIR, "data", "ftseek_us.db")
REPORT_PATH = os.path.join(PROJECT_DIR, "data", "symbol_performance.json")


def analyze_symbols(days=14):
    """分析最近 N 天的标的表现"""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    query = """
    SELECT 
        symbol,
        direction,
        COUNT(*) as trades,
        SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
        ROUND(100.0 * SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) / COUNT(*), 1) as wr_pct,
        ROUND(SUM(realized_pnl), 2) as total_pnl,
        ROUND(AVG(realized_pnl), 2) as avg_pnl,
        ROUND(MAX(realized_pnl), 2) as best,
        ROUND(MIN(realized_pnl), 2) as worst,
        ROUND(AVG(holding_hours), 1) as avg_hold_hrs
    FROM trade_records 
    WHERE trade_date >= ?
      AND source_type IN ('MAIN_PIPELINE', 'ORB_ENGINE')
      AND direction != 'SHORT_PROXY_OPTION'
    GROUP BY symbol, direction
    HAVING COUNT(*) >= 3
    ORDER BY total_pnl ASC
    """

    rows = conn.execute(query, (cutoff,)).fetchall()
    conn.close()

    results = []
    for r in rows:
        entry = dict(r)
        # 分类
        if entry["trades"] >= 5 and entry["wr_pct"] < 35 and entry["total_pnl"] < -200:
            entry["verdict"] = "DISABLE"  # 明确负 EV
        elif entry["trades"] >= 5 and entry["wr_pct"] > 45 and entry["total_pnl"] > 0:
            entry["verdict"] = "KEEP"  # 明确正 EV
        elif entry["trades"] >= 5 and entry["total_pnl"] > 0:
            entry["verdict"] = "KEEP"  # 盈利即可
        else:
            entry["verdict"] = "WATCH"  # 样本不足或边界情况
        results.append(entry)

    return results


def generate_report(results):
    """生成人类可读报告"""
    lines = []
    lines.append("📊 标的表现报告 (14天主线交易)")
    lines.append("=" * 50)

    disable = [r for r in results if r["verdict"] == "DISABLE"]
    keep = [r for r in results if r["verdict"] == "KEEP"]
    watch = [r for r in results if r["verdict"] == "WATCH"]

    if disable:
        lines.append(f"\n🔴 建议禁用 ({len(disable)} 只):")
        for r in disable:
            lines.append(
                f"  {r['symbol']} {r['direction']} | "
                f"{r['trades']}笔 {r['wr_pct']}% WR | "
                f"PnL=${r['total_pnl']:,.0f} avg=${r['avg_pnl']:.0f}"
            )

    if keep:
        lines.append(f"\n🟢 保留 ({len(keep)} 只):")
        for r in keep:
            lines.append(
                f"  {r['symbol']} {r['direction']} | "
                f"{r['trades']}笔 {r['wr_pct']}% WR | "
                f"PnL=${r['total_pnl']:,.0f} avg=${r['avg_pnl']:.0f}"
            )

    if watch:
        lines.append(f"\n🟡 观察 ({len(watch)} 只):")
        for r in watch:
            lines.append(
                f"  {r['symbol']} {r['direction']} | "
                f"{r['trades']}笔 {r['wr_pct']}% WR | "
                f"PnL=${r['total_pnl']:,.0f} avg=${r['avg_pnl']:.0f}"
            )

    # 汇总
    total_trades = sum(r["trades"] for r in results)
    total_pnl = sum(r["total_pnl"] for r in results)
    lines.append(f"\n{'=' * 50}")
    lines.append(f"合计: {total_trades}笔 | PnL=${total_pnl:,.0f}")
    lines.append(f"禁用: {len(disable)} | 保留: {len(keep)} | 观察: {len(watch)}")

    return "\n".join(lines)


def main():
    if not os.path.exists(DB_PATH):
        print(f"DB not found: {DB_PATH}")
        sys.exit(1)

    results = analyze_symbols()
    report = generate_report(results)

    # 保存 JSON
    with open(REPORT_PATH, "w") as f:
        json.dump({
            "generated_at": datetime.now().isoformat(),
            "period_days": 14,
            "symbols": results,
        }, f, indent=2, ensure_ascii=False)

    print(report)
    print(f"\n💾 已保存: {REPORT_PATH}")

    # 尝试发送飞书通知
    try:
        sys.path.insert(0, PROJECT_DIR)
        from app.notifier.feishu import send_text
        send_text(report)
        print("📤 飞书推送成功")
    except Exception as e:
        print(f"📤 飞书推送失败 (不影响报告): {e}")


if __name__ == "__main__":
    main()
