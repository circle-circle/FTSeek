#!/usr/bin/env python3
"""
RFC-055 验证脚本 — T+1/7/30 天效果评估
用法: python3 scripts/verify_rfc055.py [--days 7]
"""
import sqlite3
import argparse
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "ftseek_us.db"

def run_query(conn, sql, params=None):
    cur = conn.cursor()
    cur.execute(sql, params or [])
    return cur.fetchall()

def print_table(title, rows, headers):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")
    if not rows:
        print("  (无数据)")
        return
    widths = [len(h) for h in headers]
    for row in rows:
        for i, v in enumerate(row):
            widths[i] = max(widths[i], len(str(v)))
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print("-" * (sum(widths) + 2 * (len(widths) - 1)))
    for row in rows:
        print(fmt.format(*[str(v) for v in row]))

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7, help="回看天数")
    parser.add_argument("--db", type=str, default=str(DB_PATH))
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)

    # 1. 总体概览
    rows = run_query(conn, """
        SELECT COUNT(*) as n,
               ROUND(SUM(realized_pnl), 2) as total_pnl,
               ROUND(AVG(realized_pnl), 2) as avg_pnl,
               ROUND(100.0 * SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) / COUNT(*), 1) as win_rate,
               ROUND(SUM(CASE WHEN realized_pnl > 0 THEN realized_pnl ELSE 0 END), 2) as gross_profit,
               ROUND(SUM(CASE WHEN realized_pnl < 0 THEN realized_pnl ELSE 0 END), 2) as gross_loss
        FROM trade_records
        WHERE realized_pnl IS NOT NULL
          AND date(entry_time) >= date('now', ?)
    """, [f"-{args.days} days"])
    print_table(f"总体概览 (近 {args.days} 天)", rows,
                ["交易数", "总PnL", "均PnL", "胜率%", "毛利", "毛损"])

    # 2. 按方向
    rows = run_query(conn, """
        SELECT direction, COUNT(*) as n,
               ROUND(100.0 * SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) / COUNT(*), 1) as wr,
               ROUND(AVG(realized_pnl), 2) as avg_pnl,
               ROUND(SUM(realized_pnl), 2) as total_pnl
        FROM trade_records
        WHERE realized_pnl IS NOT NULL
          AND date(entry_time) >= date('now', ?)
        GROUP BY direction ORDER BY total_pnl DESC
    """, [f"-{args.days} days"])
    print_table("按方向", rows, ["方向", "交易数", "胜率%", "均PnL", "总PnL"])

    # 3. 按持仓时间
    rows = run_query(conn, """
        SELECT
          CASE
            WHEN CAST((julianday(exit_time) - julianday(entry_time)) * 1440 AS INT) < 5 THEN 'A: <5min'
            WHEN CAST((julianday(exit_time) - julianday(entry_time)) * 1440 AS INT) < 20 THEN 'B: 5-20min'
            WHEN CAST((julianday(exit_time) - julianday(entry_time)) * 1440 AS INT) < 60 THEN 'C: 20-60min'
            WHEN CAST((julianday(exit_time) - julianday(entry_time)) * 1440 AS INT) < 360 THEN 'D: 1-6hr'
            ELSE 'E: >6hr'
          END as hold_bucket,
          COUNT(*) as n,
          ROUND(100.0 * SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) / COUNT(*), 1) as wr,
          ROUND(AVG(realized_pnl), 2) as avg_pnl,
          ROUND(SUM(realized_pnl), 2) as total_pnl
        FROM trade_records
        WHERE realized_pnl IS NOT NULL AND exit_time IS NOT NULL
          AND date(entry_time) >= date('now', ?)
        GROUP BY hold_bucket ORDER BY hold_bucket
    """, [f"-{args.days} days"])
    print_table("按持仓时间", rows, ["时段", "交易数", "胜率%", "均PnL", "总PnL"])

    # 4. 按退出原因
    rows = run_query(conn, """
        SELECT COALESCE(exit_reason, 'UNKNOWN') as reason,
               COUNT(*) as n,
               ROUND(100.0 * SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) / COUNT(*), 1) as wr,
               ROUND(AVG(realized_pnl), 2) as avg_pnl,
               ROUND(SUM(realized_pnl), 2) as total_pnl
        FROM trade_records
        WHERE realized_pnl IS NOT NULL
          AND date(entry_time) >= date('now', ?)
        GROUP BY reason ORDER BY total_pnl DESC
    """, [f"-{args.days} days"])
    print_table("按退出原因", rows, ["原因", "交易数", "胜率%", "均PnL", "总PnL"])

    # 5. 按标的
    rows = run_query(conn, """
        SELECT symbol, COUNT(*) as n,
               ROUND(100.0 * SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) / COUNT(*), 1) as wr,
               ROUND(SUM(realized_pnl), 2) as total_pnl
        FROM trade_records
        WHERE realized_pnl IS NOT NULL
          AND date(entry_time) >= date('now', ?)
        GROUP BY symbol HAVING COUNT(*) >= 2
        ORDER BY total_pnl DESC LIMIT 10
    """, [f"-{args.days} days"])
    print_table("Top 10 标的", rows, ["标的", "交易数", "胜率%", "总PnL"])

    # 6. 黑名单标的检查 (确认无新交易)
    rows = run_query(conn, """
        SELECT symbol, COUNT(*) as n, date(MAX(entry_time)) as last_trade
        FROM trade_records
        WHERE symbol IN ('DIA', 'NVDA', 'LCID', 'TME', 'UBER', 'QCOM', 'NIO')
          AND date(entry_time) >= date('now', ?)
        GROUP BY symbol
    """, [f"-{args.days} days"])
    print_table("黑名单标的交易 (应为空)", rows, ["标的", "交易数", "最后交易日"])

    # 7. 日均收益
    rows = run_query(conn, """
        SELECT date(entry_time) as day, COUNT(*) as n,
               ROUND(SUM(realized_pnl), 2) as daily_pnl,
               ROUND(100.0 * SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) / COUNT(*), 1) as wr
        FROM trade_records
        WHERE realized_pnl IS NOT NULL
          AND date(entry_time) >= date('now', ?)
        GROUP BY day ORDER BY day
    """, [f"-{args.days} days"])
    print_table("每日明细", rows, ["日期", "交易数", "日PnL", "胜率%"])

    # 8. RFC-055 关键指标检查
    print(f"\n{'='*60}")
    print("  RFC-055 关键指标检查")
    print(f"{'='*60}")

    # 检查 <20min 交易占比
    rows = run_query(conn, """
        SELECT
          SUM(CASE WHEN CAST((julianday(exit_time)-julianday(entry_time))*1440 AS INT) < 20 THEN 1 ELSE 0 END) as short_trades,
          COUNT(*) as total,
          ROUND(100.0 * SUM(CASE WHEN CAST((julianday(exit_time)-julianday(entry_time))*1440 AS INT) < 20 THEN 1 ELSE 0 END) / COUNT(*), 1) as short_pct
        FROM trade_records
        WHERE realized_pnl IS NOT NULL AND exit_time IS NOT NULL
          AND date(entry_time) >= date('now', ?)
    """, [f"-{args.days} days"])
    if rows and rows[0][0] is not None:
        short, total, pct = rows[0]
        status = "✅" if pct < 20 else "⚠️" if pct < 40 else "🔴"
        print(f"  {status} <20min交易占比: {pct}% ({short}/{total}) [目标<20%]")

    # 检查 >6hr 交易占比
    rows = run_query(conn, """
        SELECT
          SUM(CASE WHEN CAST((julianday(exit_time)-julianday(entry_time))*1440 AS INT) > 360 THEN 1 ELSE 0 END) as long_trades,
          COUNT(*) as total
        FROM trade_records
        WHERE realized_pnl IS NOT NULL AND exit_time IS NOT NULL
          AND date(entry_time) >= date('now', ?)
    """, [f"-{args.days} days"])
    if rows and rows[0][0] is not None:
        long_t, total = rows[0]
        pct = round(100.0 * long_t / total, 1) if total > 0 else 0
        status = "✅" if pct < 10 else "⚠️" if pct < 25 else "🔴"
        print(f"  {status} >6hr交易占比: {pct}% ({long_t}/{total}) [目标<10%]")

    # 检查 LONG vs SHORT 比例
    rows = run_query(conn, """
        SELECT direction, COUNT(*) as n
        FROM trade_records
        WHERE realized_pnl IS NOT NULL
          AND date(entry_time) >= date('now', ?)
        GROUP BY direction
    """, [f"-{args.days} days"])
    dir_map = {r[0]: r[1] for r in rows}
    long_n = dir_map.get('LONG', 0)
    short_n = dir_map.get('SHORT', 0)
    total = long_n + short_n
    if total > 0:
        short_pct = round(100.0 * short_n / total, 1)
        status = "✅" if short_pct >= 55 else "⚠️" if short_pct >= 40 else "🔴"
        print(f"  {status} SHORT占比: {short_pct}% ({short_n}/{total}) [目标≥55%]")

    # 检查主线 (排除scalping/options) 胜率
    rows = run_query(conn, """
        SELECT COUNT(*) as n,
               ROUND(100.0 * SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) / COUNT(*), 1) as wr,
               ROUND(SUM(realized_pnl), 2) as total_pnl
        FROM trade_records
        WHERE realized_pnl IS NOT NULL
          AND date(entry_time) >= date('now', ?)
          AND COALESCE(owner_strategy, '') NOT IN ('SCALPING', 'SHORT_PROXY_OPTION')
    """, [f"-{args.days} days"])
    if rows and rows[0][0]:
        n, wr, pnl = rows[0]
        status = "✅" if wr >= 45 else "⚠️" if wr >= 38 else "🔴"
        print(f"  {status} 主线胜率: {wr}% ({n}笔, PnL=${pnl}) [目标≥45%]")

    conn.close()
    print(f"\n{'='*60}")
    print("  验证完成")
    print(f"{'='*60}")

if __name__ == "__main__":
    main()
