#!/usr/bin/env python3
"""
Shadow vs Real 对照分析 CLI (Z3, 2026-05-15)

基于 5/14 复盘发现 shadow 与 real 样本不可比的问题, 提供 1:1 对照视图.

用法:
  python scripts/shadow_compare.py                  # 今日对照
  python scripts/shadow_compare.py --days 7         # 7 天滚动
  python scripts/shadow_compare.py --variant baseline  # 仅 baseline 变体
  python scripts/shadow_compare.py --by-symbol      # 按 symbol 分解
"""
from __future__ import annotations
import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def cmd_overview(days: int):
    print(f"\n[{datetime.now().isoformat(timespec='seconds')}] Shadow vs Real ({days}d)")
    print("=" * 70)
    try:
        from app.core.paths import get_trade_records_db
        conn = sqlite3.connect(str(get_trade_records_db()))
        cutoff = (datetime.now() - timedelta(days=days)).date().isoformat()

        # Real
        real = conn.execute("""
            SELECT COUNT(*) n, ROUND(AVG(realized_pnl),2) avg,
                   ROUND(SUM(realized_pnl),2) total,
                   ROUND(AVG(CASE WHEN realized_pnl>0 THEN 1.0 ELSE 0.0 END)*100,1) wr
            FROM trade_records
            WHERE date(exit_time) >= ? AND realized_pnl IS NOT NULL
        """, (cutoff,)).fetchone()

        # Shadow by variant
        shadow_rows = conn.execute("""
            SELECT strategy as variant, COUNT(*) n,
                   ROUND(AVG(realized_pnl),2) avg,
                   ROUND(SUM(realized_pnl),2) total,
                   ROUND(AVG(CASE WHEN realized_pnl>0 THEN 1.0 ELSE 0.0 END)*100,1) wr
            FROM shadow_trades
            WHERE date(COALESCE(exit_time, entry_time)) >= ? AND closed = 1
            GROUP BY strategy ORDER BY total DESC
        """, (cutoff,)).fetchall()

        print(f"\n  {'mode':25s} {'n':>5s} {'avg':>10s} {'total':>12s} {'win%':>8s}")
        print("  " + "-" * 65)
        print(f"  {'REAL':25s} {real[0]:>5d} ${real[1] or 0:>9.2f} ${real[2] or 0:>11.2f} {real[3] or 0:>7.1f}%")
        print()
        if not shadow_rows:
            print("  [SHADOW]  无 closed 数据 (Z1 镜像可能尚未生效, 重启后验证)")
        else:
            for v, n, avg, total, wr in shadow_rows:
                marker = "📊" if v == "baseline" else "🧪"
                print(f"  {marker} {('shadow:'+v):22s} {n:>5d} ${avg or 0:>9.2f} ${total or 0:>11.2f} {wr or 0:>7.1f}%")

        # 解读
        print()
        print("=== 解读 ===")
        print("  baseline: 同实盘信号、同止损 → 用于验证 shadow 系统是否准确镜像 real")
        print("  其他变体: 假设性参数 → 直接对比 real, 评估参数变更收益")

        conn.close()
    except Exception as e:
        print(f"  [ERR] {e}")


def cmd_by_symbol(days: int):
    """按 symbol 分解 — 找出哪些标的的 shadow 与 real 偏离大"""
    print(f"\n=== 按 Symbol 对比 (过去 {days}d) ===")
    try:
        from app.core.paths import get_trade_records_db
        conn = sqlite3.connect(str(get_trade_records_db()))
        cutoff = (datetime.now() - timedelta(days=days)).date().isoformat()

        # Real by symbol
        real = {r[0]: (r[1], r[2]) for r in conn.execute("""
            SELECT symbol, COUNT(*), ROUND(SUM(realized_pnl),2)
            FROM trade_records WHERE date(exit_time) >= ? AND realized_pnl IS NOT NULL
            GROUP BY symbol
        """, (cutoff,)).fetchall()}

        # Shadow baseline by symbol
        shadow = {r[0]: (r[1], r[2]) for r in conn.execute("""
            SELECT symbol, COUNT(*), ROUND(SUM(realized_pnl),2)
            FROM shadow_trades
            WHERE date(COALESCE(exit_time, entry_time)) >= ? AND closed = 1
              AND strategy = 'baseline'
            GROUP BY symbol
        """, (cutoff,)).fetchall()}

        all_syms = sorted(set(real.keys()) | set(shadow.keys()))
        if not all_syms:
            print("  (无数据)")
            return

        print(f"\n  {'symbol':12s} {'real_n':>7s} {'real_pnl':>10s} {'sh_n':>5s} {'sh_pnl':>10s} {'对照可行':>10s}")
        for sym in all_syms:
            r = real.get(sym, (0, 0))
            s = shadow.get(sym, (0, 0))
            ok = "✅" if r[0] > 0 and s[0] > 0 and abs(r[0] - s[0]) <= 1 else "❌"
            print(f"  {sym:12s} {r[0]:>7d} ${r[1] or 0:>9.2f} {s[0]:>5d} ${s[1] or 0:>9.2f}  {ok}")

        conn.close()
    except Exception as e:
        print(f"  [ERR] {e}")


def cmd_variant_diff(days: int):
    """对比不同 variant 的表现"""
    print(f"\n=== Variant 收益差异 (过去 {days}d) ===")
    try:
        from app.core.paths import get_trade_records_db
        conn = sqlite3.connect(str(get_trade_records_db()))
        cutoff = (datetime.now() - timedelta(days=days)).date().isoformat()

        rows = conn.execute("""
            SELECT strategy variant, COUNT(*) n,
                   ROUND(AVG(realized_pnl),2) avg,
                   ROUND(SUM(realized_pnl),2) total,
                   ROUND(AVG(realized_pnl) - (SELECT AVG(realized_pnl) FROM shadow_trades WHERE strategy='baseline' AND closed=1 AND date(exit_time) >= ?), 2) avg_diff_vs_baseline
            FROM shadow_trades
            WHERE closed = 1 AND date(COALESCE(exit_time, entry_time)) >= ?
            GROUP BY strategy ORDER BY total DESC
        """, (cutoff, cutoff)).fetchall()
        if not rows:
            print("  (无数据)")
            return

        print(f"\n  {'variant':25s} {'n':>5s} {'avg':>10s} {'total':>12s} {'vs_baseline':>13s}")
        print("  " + "-" * 70)
        for v, n, avg, total, diff in rows:
            mark = "✅" if (diff or 0) > 0 else ("📊" if v == "baseline" else "")
            print(f"  {mark} {v:23s} {n:>5d} ${avg or 0:>9.2f} ${total or 0:>11.2f} {('$'+str(diff or 0)):>13s}")

        conn.close()
    except Exception as e:
        print(f"  [ERR] {e}")


def cmd_t_test(days: int):
    """统计显著性测试 — 各 variant vs baseline 是否真的不同 (p < 0.05).

    避免 outlier 主导误判: 即使 variant 平均 PnL 看起来好, t-test 也可能 p > 0.05 (不显著).

    用 Welch's t-test (不假设方差相等), 不依赖 scipy (纯 Python 实现).
    """
    print(f"\n=== Variant vs Baseline t-test (过去 {days}d) ===")
    try:
        from app.core.paths import get_trade_records_db
        conn = sqlite3.connect(str(get_trade_records_db()))
        cutoff = (datetime.now() - timedelta(days=days)).date().isoformat()

        # 按 variant 拉所有 closed PnL 序列
        rows = conn.execute("""
            SELECT strategy, realized_pnl
            FROM shadow_trades
            WHERE closed = 1 AND date(COALESCE(exit_time, entry_time)) >= ?
              AND realized_pnl IS NOT NULL
        """, (cutoff,)).fetchall()
        conn.close()

        from collections import defaultdict
        groups = defaultdict(list)
        for variant, pnl in rows:
            groups[variant].append(float(pnl))

        baseline = groups.get("baseline", [])
        if not baseline:
            print("  ⚠️ 无 baseline 数据, 无法 t-test")
            return
        if len(baseline) < 5:
            print(f"  ⚠️ baseline n={len(baseline)} < 5, 统计不充分")

        print(f"\n  baseline: n={len(baseline)} sum=${sum(baseline):.0f} avg=${sum(baseline)/len(baseline):.2f}")
        print()
        print(f"  {'variant':25s} {'n':>5s} {'avg':>10s} {'avg_diff':>10s} {'t_stat':>8s} {'p_value':>10s} {'sig':>6s}")
        print("  " + "-" * 80)

        for variant, pnls in sorted(groups.items()):
            if variant == "baseline":
                continue
            n = len(pnls)
            if n < 3:
                print(f"  {variant:25s} {n:>5d}  (n<3, 跳过)")
                continue

            t, p = _welch_t_test(pnls, baseline)
            avg = sum(pnls) / n
            avg_diff = avg - sum(baseline) / len(baseline)
            sig = "✅⭐" if p < 0.05 and avg_diff > 0 else \
                  "🔴" if p < 0.05 and avg_diff < 0 else \
                  "—"
            print(f"  {variant:25s} {n:>5d} ${avg:>9.2f} ${avg_diff:>+9.2f} "
                  f"{t:>+8.2f} {p:>10.3f} {sig:>6s}")

        print()
        print("  显著性: p < 0.05 = 95% 置信; ⭐ = 显著优于 baseline; 🔴 = 显著劣于")
    except Exception as e:
        print(f"  [ERR] {e}")


def _welch_t_test(a: list, b: list) -> tuple:
    """Welch's t-test (不依赖 scipy, 纯 Python).

    Returns: (t_statistic, p_value)
    """
    import math
    n_a, n_b = len(a), len(b)
    if n_a < 2 or n_b < 2:
        return 0.0, 1.0
    mean_a = sum(a) / n_a
    mean_b = sum(b) / n_b
    # 样本方差 (n-1)
    var_a = sum((x - mean_a) ** 2 for x in a) / (n_a - 1)
    var_b = sum((x - mean_b) ** 2 for x in b) / (n_b - 1)
    # 标准误
    se = math.sqrt(var_a / n_a + var_b / n_b)
    if se == 0:
        return 0.0, 1.0
    t = (mean_a - mean_b) / se
    # 自由度 (Welch-Satterthwaite)
    if var_a == 0 and var_b == 0:
        return 0.0, 1.0
    df_num = (var_a / n_a + var_b / n_b) ** 2
    df_den = (var_a / n_a) ** 2 / (n_a - 1) + (var_b / n_b) ** 2 / (n_b - 1)
    df = df_num / df_den if df_den > 0 else n_a + n_b - 2
    # 双尾 p-value (近似, 用 normal 分布近似 large-df t 分布)
    # 大样本 (df > 30) t ≈ z, 用 erfc
    z = abs(t)
    p = math.erfc(z / math.sqrt(2))  # 双尾
    return t, p


def cmd_recent_pairs(n: int):
    """显示最近 N 笔实盘 + 对应 shadow 镜像的明细"""
    print(f"\n=== 最近 {n} 笔实盘 + Shadow 镜像 ===")
    try:
        from app.core.paths import get_trade_records_db
        conn = sqlite3.connect(str(get_trade_records_db()))
        rows = conn.execute("""
            SELECT t.trace_id, t.symbol, t.direction, t.entry_time, t.exit_time,
                   ROUND(t.realized_pnl, 2) real_pnl, t.exit_reason
            FROM trade_records t
            WHERE t.exit_time IS NOT NULL AND t.realized_pnl IS NOT NULL
            ORDER BY t.exit_time DESC LIMIT ?
        """, (n,)).fetchall()

        for trace_id, sym, dir_, ent, ext, pnl, reason in rows:
            print(f"\n  {sym} {dir_} {ent} → {ext}  real={pnl:+.2f}  reason={reason}")
            shadow_rows = conn.execute("""
                SELECT strategy, ROUND(realized_pnl, 2), exit_reason
                FROM shadow_trades
                WHERE signal_detail LIKE ? AND closed = 1
            """, (f'%{trace_id}%',)).fetchall()
            if not shadow_rows:
                print("    (无对应镜像)")
            else:
                for variant, sh_pnl, sh_reason in shadow_rows:
                    diff = (sh_pnl or 0) - (pnl or 0)
                    mark = "✅" if diff > 0 else "❌" if diff < 0 else "="
                    print(f"    {mark} shadow:{variant:18s} pnl={sh_pnl:+.2f}  diff={diff:+.2f}  reason={sh_reason or '?'}")
        conn.close()
    except Exception as e:
        print(f"  [ERR] {e}")


def main():
    ap = argparse.ArgumentParser(description="Shadow vs Real 对照分析")
    ap.add_argument("--days", type=int, default=1, help="回看天数 (默认 1)")
    ap.add_argument("--by-symbol", action="store_true", help="按 symbol 分解")
    ap.add_argument("--variant-diff", action="store_true", help="variant 收益差异")
    ap.add_argument("--t-test", action="store_true", help="统计显著性测试 (新增 2026-05-19)")
    ap.add_argument("--recent", type=int, default=0, help="最近 N 笔明细")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    cmd_overview(args.days)
    if args.all or args.by_symbol:
        cmd_by_symbol(args.days)
    if args.all or args.variant_diff:
        cmd_variant_diff(args.days)
    if args.all or args.t_test:
        cmd_t_test(args.days)
    if args.recent > 0 or args.all:
        cmd_recent_pairs(args.recent or 5)


if __name__ == "__main__":
    main()
