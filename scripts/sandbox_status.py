#!/usr/bin/env python3
"""
Sandbox 模式状态 + 时间序列对比 (SB5 / P1-D 2026-05-15)

用法:
  python scripts/sandbox_status.py                 # 当前状态 + 今日数据
  python scripts/sandbox_status.py --compare       # 对比 normal vs sandbox PnL
  python scripts/sandbox_status.py --guard         # SymbolDailyLossGuard 触发情况
  python scripts/sandbox_status.py --all           # 全部
"""
from __future__ import annotations
import argparse
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def cmd_status():
    print(f"\n[{datetime.now().isoformat(timespec='seconds')}] Sandbox 状态")
    print("=" * 70)
    try:
        from app.core.config_container import get_config
        cfg = get_config()
        enabled = cfg.get_dotted("mode.sandbox.enabled", False)
        universe = cfg.get_dotted("mode.sandbox.universe", []) or []
        overrides = cfg.get_dotted("mode.sandbox.overrides", {}) or {}
        print(f"  mode: {'🧪 SANDBOX' if enabled else '🟢 NORMAL'}")
        print(f"  universe ({len(universe)}): {universe}")
        print()
        print("  风控放宽 (vs 标准):")
        for k, v in overrides.items():
            print(f"    {k:50s} = {v}")
    except Exception as e:
        print(f"  [ERR] 读 yaml 失败: {e}")


def cmd_today_breakdown():
    print("\n=== 今日交易 (按 mode 分层) ===")
    try:
        from app.core.paths import get_trade_records_db
        conn = sqlite3.connect(str(get_trade_records_db()))
        rows = conn.execute("""
            SELECT COALESCE(mode,'normal') as m, COUNT(*) as n,
                   ROUND(AVG(realized_pnl),2) as avg_pnl,
                   ROUND(SUM(realized_pnl),2) as total_pnl,
                   ROUND(AVG(CASE WHEN realized_pnl>0 THEN 1.0 ELSE 0.0 END)*100,1) as win_rate
            FROM trade_records
            WHERE date(exit_time) = date('now') AND exit_time IS NOT NULL
            GROUP BY 1 ORDER BY n DESC
        """).fetchall()
        if not rows:
            print("  (今日尚无平仓交易)")
        else:
            print(f"  {'mode':10s} {'n':>5s} {'avg':>10s} {'total':>12s} {'win_rate':>10s}")
            for m, n, a, t, wr in rows:
                print(f"  {m:10s} {n:>5d} ${a:>9.2f} ${t:>11.2f} {wr:>9.1f}%")
        conn.close()
    except Exception as e:
        print(f"  [ERR] 查询失败: {e}")


def cmd_compare(days: int = 7):
    print(f"\n=== 时间序列对比 (前 {days}d normal vs 后 {days}d sandbox) ===")
    try:
        from app.core.paths import get_trade_records_db
        conn = sqlite3.connect(str(get_trade_records_db()))
        cutoff = (datetime.now() - timedelta(days=days)).date().isoformat()
        rows = conn.execute("""
            SELECT
                CASE WHEN date(exit_time) >= ? THEN 'recent_'||COALESCE(mode,'normal')
                     ELSE 'prior_normal' END as bucket,
                COUNT(*) as n,
                ROUND(AVG(realized_pnl),2) as avg_pnl,
                ROUND(SUM(realized_pnl),2) as total,
                ROUND(AVG(CASE WHEN realized_pnl>0 THEN 1.0 ELSE 0.0 END)*100,1) as win_rate
            FROM trade_records
            WHERE date(exit_time) >= date('now', ?)
              AND exit_time IS NOT NULL
            GROUP BY 1
        """, (cutoff, f'-{days*2} days')).fetchall()
        if rows:
            print(f"  {'bucket':25s} {'n':>5s} {'avg':>10s} {'total':>12s} {'win_rate':>10s}")
            for b, n, a, t, wr in rows:
                print(f"  {b:25s} {n:>5d} ${a or 0:>9.2f} ${t or 0:>11.2f} {wr or 0:>9.1f}%")
        else:
            print("  (无数据)")
        conn.close()
    except Exception as e:
        print(f"  [ERR] {e}")


def cmd_guard():
    """SymbolDailyLossGuard 状态 (需服务在线读内存; 离线只能读 DB 推断)"""
    print("\n=== SymbolDailyLossGuard 当日触发 (从 trade_records 推断) ===")
    try:
        from app.core.paths import get_trade_records_db
        conn = sqlite3.connect(str(get_trade_records_db()))
        rows = conn.execute("""
            SELECT symbol,
                   COUNT(*) as n,
                   ROUND(SUM(realized_pnl),2) as total_pnl,
                   ROUND(AVG(CASE WHEN realized_pnl<0 THEN 1.0 ELSE 0.0 END)*100,0) as loss_rate
            FROM trade_records
            WHERE date(exit_time) = date('now') AND exit_time IS NOT NULL
            GROUP BY symbol HAVING n >= 2
            ORDER BY total_pnl
        """).fetchall()
        conn.close()
        if not rows:
            print("  (今日无足够交易)")
            return
        print(f"  {'symbol':12s} {'n':>4s} {'total':>10s} {'loss_rate':>10s}  {'should_block?':15s}")
        for sym, n, total, lr in rows:
            should_block = (n >= 3 and lr >= 70) or (total or 0) <= -1000
            mark = "🚫 YES" if should_block else "✅ ok"
            print(f"  {sym:12s} {n:>4d} ${total or 0:>9.2f} {lr or 0:>9.0f}%  {mark}")
    except Exception as e:
        print(f"  [ERR] {e}")


def cmd_critical_flag():
    """检查 critical_alerts.flag 是否存在"""
    print("\n=== 关键告警 flag 检查 ===")
    try:
        from app.utils.critical_alerts import _flag_path, has_pending_critical
        flag = _flag_path()
        if has_pending_critical():
            print(f"  ⚠️ 存在未送达告警: {flag}")
            print("  内容:")
            try:
                content = open(flag).read()[:500]
                for line in content.split("\n"):
                    print(f"    {line}")
            except Exception:
                pass
        else:
            print(f"  ✅ 无 pending 告警 ({flag} 不存在)")
    except Exception as e:
        print(f"  [ERR] {e}")


def main():
    ap = argparse.ArgumentParser(description="Sandbox 模式状态 + 对比 (SB5 / P1-D)")
    ap.add_argument("--compare", action="store_true", help="时间序列对比 (前 7 天 vs 后 7 天)")
    ap.add_argument("--days", type=int, default=7, help="对比窗口天数")
    ap.add_argument("--guard", action="store_true", help="SymbolDailyLossGuard 当日状态")
    ap.add_argument("--all", action="store_true", help="全部")
    args = ap.parse_args()

    cmd_status()
    cmd_today_breakdown()
    if args.all or args.compare:
        cmd_compare(days=args.days)
    if args.all or args.guard:
        cmd_guard()
    if args.all:
        cmd_critical_flag()


if __name__ == "__main__":
    main()
