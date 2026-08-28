#!/usr/bin/env python3
"""
ADR-004 (2026-06-04): 标的表现周报 — 自动生成 + 飞书推送

分析最近 N 天 (默认 14) playbook 交易数据 (白名单 PLAYBOOK_SIM/PLAYBOOK_LIVE, 净口径)，
按标的聚合统计：
- 总交易数、胜率、累计盈亏、平均盈亏
- 标注正 EV / 负 EV / 样本不足
- 建议禁用/保留/观察

数据驱动，不靠人猜。
Traceback 预防: sys.path 必须在 `from scripts.utils...` 之前注入 (07 月三周 cron 崩溃即此因)。

口径修正 (2026-08-28, 全仓"入场日记账"清扫的最后一个报告消费者):
  · 窗口按**出场 ET 日** substr(exit_time,1,10) 归集, 不再用 trade_date (=入场日) —
    隔夜/跨周才了结的仓会算进错误的窗口 (盈亏按了结日归属是全仓统一尺)。
  · 窗口起点取 **ET** 今天往前推, 服务器是 CST (UTC+8) — 用 CST 日切会让窗口少/多一天。
  · 只算已了结 exit_price > 0 — 在仓行的 NULL 盈亏原本照样计笔数, 既稀释胜率分母,
    又能把"1 笔在仓 + 2 笔了结"的标的抬进 MIN_TRADES=3 的裁决门槛。
  · 删掉 `trade_mode != 'live'` — 与来源白名单自相矛盾: 放行 PLAYBOOK_LIVE 却又按
    mode 把真实盘行剔除, 将来开始实盘时最该进报告的仓会被静默隐藏 (现库内该谓词
    命中 0 行, 删除行为中性)。
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)
DB_PATH = os.path.join(PROJECT_DIR, "data", "ftseek_us.db")
REPORT_PATH = os.path.join(PROJECT_DIR, "data", "symbol_performance.json")
MIN_TRADES = 3   # 低于此样本量的 (标的×方向) 组不进裁决, 但会在报告里报数


def _today_et() -> date:
    """ET 今天 — CST 直取会在 CST 12:00-20:00 段比 ET 多一天."""
    try:
        from app.utils.timezone import now_et
        return now_et().date()
    except Exception as e:  # 时区库异常不该让周报消失
        print(f"[symbol_performance] now_et 不可用, 回落服务器本地日: {e}", file=sys.stderr)
        return date.today()


def analyze_symbols(days=14, db_path=DB_PATH):
    """分析最近 N 天的标的表现 (出场日归集 + 净额 + 仅已了结).

    Returns: (results, dropped) — results 为样本量达标的裁决行, dropped 为样本不足的组。
    """
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    cutoff = (_today_et() - timedelta(days=max(1, int(days)) - 1)).isoformat()
    query = """
    SELECT
        symbol,
        direction,
        COUNT(*) as trades,
        SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) as wins,
        ROUND(100.0 * SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) / COUNT(*), 1) as wr_pct,
        ROUND(COALESCE(SUM(net_pnl), 0), 2) as total_pnl,
        ROUND(COALESCE(AVG(net_pnl), 0), 2) as avg_pnl,
        ROUND(MAX(net_pnl), 2) as best,
        ROUND(MIN(net_pnl), 2) as worst,
        ROUND(AVG(holding_hours), 1) as avg_hold_hrs
    FROM (SELECT *, COALESCE(pnl_net, realized_pnl) AS net_pnl FROM trade_records
          WHERE substr(exit_time, 1, 10) >= ?
            AND exit_price > 0
            AND source_type IN ('PLAYBOOK_SIM', 'PLAYBOOK_LIVE')
            AND direction != 'SHORT_PROXY_OPTION')
    GROUP BY symbol, direction
    ORDER BY total_pnl ASC
    """

    rows = [dict(r) for r in conn.execute(query, (cutoff,)).fetchall()]
    conn.close()

    results = []
    dropped = []
    for entry in rows:
        if entry["trades"] < MIN_TRADES:
            dropped.append(entry)   # 样本不足 → 不给裁决, 但报告必须报数 (原先整组静默消失)
            continue
        # 分类
        if entry["trades"] >= 5 and entry["wr_pct"] < 35 and entry["total_pnl"] < -200:
            entry["verdict"] = "DISABLE"  # 明确负 EV
        elif entry["trades"] >= 5 and entry["wr_pct"] > 45 and entry["total_pnl"] > 0:
            entry["verdict"] = "KEEP"  # 明确正 EV
        elif entry["trades"] >= 5 and entry["total_pnl"] > 0:
            entry["verdict"] = "KEEP"  # 盈利即可
        else:
            entry["verdict"] = "WATCH"  # 边界情况 (n=3~4 或盈亏恰在阈值上)
        results.append(entry)

    return results, dropped


def generate_report(results, dropped=(), days=14, window=None):
    """生成人类可读报告"""
    lines = []
    span = f" ({window[0]} ~ {window[1]}, 出场日ET)" if window else ""
    lines.append(f"📊 标的表现报告 ({days}天主线交易{span})")
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
    if dropped:
        d_trades = sum(r["trades"] for r in dropped)
        d_pnl = sum(r["total_pnl"] for r in dropped)
        lines.append(
            f"样本不足未裁决 (<{MIN_TRADES}笔): {len(dropped)} 组 / {d_trades}笔 / "
            f"PnL=${d_pnl:,.0f} (不计入上方合计)"
        )

    return "\n".join(lines)


def resolve_days(args) -> int:
    """--days 优先, 其次 --weeks*7, 都缺省=14 (crontab 无参调用保持原窗口)."""
    if args.days is not None:
        return max(1, args.days)
    if args.weeks is not None:
        return max(1, args.weeks * 7)
    return 14


def window_has_trading_day(days: int) -> bool:
    """窗口内是否存在美股交易日 — 没有就无数据可报.

    2026-08-28: 原用 scripts/utils/market_guard.exit_if_not_trading_day(days_back=1)
    ("昨天是不是交易日"), 那是为**每日**盘后脚本设计的。本脚本两个调用方都是**周级**
    聚合: ①crontab 周一 08:00 CST (为它打过 days_back=3 的补丁), ②Hermes「周日综合复盘」
    Step 4 (周日 10:00 CST 跑) — 周日语境下"昨天"恒为周六 → 恒非交易日 → 整段静默
    sys.exit(0), 即周报里的"标的表现"一节自 job 合并以来从未有过数据 (与 Step 5 同症)。
    N 天聚合的正确前提是"窗口内至少有一个交易日", 而不是"昨天开过市"。
    """
    try:
        from app.utils.holiday_calendar import get_holiday_calendar
        cal = get_holiday_calendar()
        today = _today_et()
        return any(cal.is_trading_day("US", today - timedelta(days=k)) for k in range(days))
    except Exception as e:
        print(f"[symbol_performance] 交易日检查异常 (放行): {e}")
        return True


def main(argv=None):
    ap = argparse.ArgumentParser(description="标的表现周报/自定义窗口 (出场日ET归集 + 净额口径)")
    ap.add_argument("--days", type=int, default=None, help="窗口天数 (优先于 --weeks)")
    ap.add_argument("--weeks", type=int, default=None, help="窗口周数 (= weeks*7 天)")
    ap.add_argument("--db", default=DB_PATH, help=f"SQLite 路径 (默认 {DB_PATH})")
    ap.add_argument("--out", default=REPORT_PATH, help=f"JSON 输出路径 (默认 {REPORT_PATH})")
    ap.add_argument("--no-push", action="store_true", help="只打印, 不推飞书/不写 JSON")
    args = ap.parse_args(argv)
    days = resolve_days(args)

    if not window_has_trading_day(days):
        print(f"[symbol_performance] 近 {days} 天无美股交易日, 跳过")
        return 0
    if not os.path.exists(args.db):
        print(f"DB not found: {args.db}")
        return 1

    results, dropped = analyze_symbols(days=days, db_path=args.db)
    end = _today_et()
    window = ((end - timedelta(days=days - 1)).isoformat(), end.isoformat())
    report = generate_report(results, dropped, days=days, window=window)
    print(report)

    if args.no_push:
        return 0

    # 保存 JSON
    with open(args.out, "w") as f:
        json.dump({
            "generated_at": datetime.now().isoformat(),
            "period_days": days,
            "window": {"start": window[0], "end": window[1], "caliber": "exit_et_day"},
            "symbols": results,
            "below_min_trades": dropped,
        }, f, indent=2, ensure_ascii=False)
    print(f"\n💾 已保存: {args.out}")

    # 尝试发送飞书通知
    try:
        sys.path.insert(0, PROJECT_DIR)
        from app.notifier.feishu import send_text
        send_text(report)
        print("📤 飞书推送成功")
    except Exception as e:
        print(f"📤 飞书推送失败 (不影响报告): {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
