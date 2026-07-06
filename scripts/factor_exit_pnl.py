#!/usr/bin/env python3
"""
因子×退出方式 P&L 聚合脚本

从 trade_records.metadata_json.conditions × exit_reason 聚合实际交易 P&L,
输出 data/factor_exit_pnl.json, 作为退出规则调优的依据.

与 factor_ic_latest.json 的区别:
  - factor_ic_latest: 因子 → 次日价格收益 (IC, 预测能力)
  - factor_exit_pnl:  因子 × 退出方式 → 实际交易 P&L (盈利能力)

Crontab: 45 5 * * 2-6 (在 factor_ic_backfill 后运行)
"""
import sys
import os
import sqlite3
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)
os.chdir(PROJECT_DIR)

from dotenv import load_dotenv
load_dotenv(".env.us")

DB_PATH = Path("data/ftseek_us.db")
OUTPUT_PATH = Path("data/factor_exit_pnl.json")


def main():
    from scripts.utils.market_guard import exit_if_not_trading_day
    exit_if_not_trading_day()
    print("=== 因子×退出方式 P&L 聚合 ===")

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT id, symbol, direction, entry_price, exit_price, realized_pnl, pnl_pct,
               exit_reason, holding_hours,
               json_extract(metadata_json, '$.conditions') as conditions_str,
               json_extract(metadata_json, '$.peak_price') as peak_price,
               json_extract(metadata_json, '$.cohort') as cohort,
               json_extract(metadata_json, '$.attribution.overall_score') as attr_score
        FROM trade_records
        WHERE source_type IN ('PLAYBOOK_SIM', 'PLAYBOOK_LIVE')
          AND exit_price IS NOT NULL AND exit_price > 0
          AND exit_reason IS NOT NULL AND exit_reason != ''
        ORDER BY id
    """).fetchall()

    print(f"  闭环交易: {len(rows)}条")

    if not rows:
        print("  无数据, 跳过")
        conn.close()
        return

    # ── 聚合1: 因子×退出方式 ──
    factor_exit = defaultdict(lambda: {
        'n': 0, 'pnl': 0.0, 'wins': 0, 'pnl_pct_sum': 0.0,
        'holding_hours_sum': 0.0, 'mfe_pcts': []
    })

    # ── 聚合2: 因子总览 ──
    factor_summary = defaultdict(lambda: {
        'n': 0, 'pnl': 0.0, 'wins': 0,
        'exit_breakdown': defaultdict(lambda: {'n': 0, 'pnl': 0.0, 'wins': 0})
    })

    # ── 聚合3: 退出方式总览 ──
    exit_summary = defaultdict(lambda: {
        'n': 0, 'pnl': 0.0, 'wins': 0
    })

    for r in rows:
        conditions_str = r['conditions_str'] or '[]'
        try:
            conditions = json.loads(conditions_str)
        except Exception:
            conditions = []

        if not conditions:
            conditions = ['_no_factor']

        pnl = float(r['realized_pnl'] or 0)
        pnl_pct = float(r['pnl_pct'] or 0)
        is_win = pnl > 0
        exit_reason = r['exit_reason'] or 'UNKNOWN'
        holding_hours = float(r['holding_hours'] or 0)

        # MFE: peak_price vs entry
        mfe_pct = 0.0
        peak_price = r['peak_price']
        entry_price = float(r['entry_price'] or 0)
        if peak_price and entry_price > 0:
            peak_price = float(peak_price)
            if r['direction'] == 'LONG':
                mfe_pct = (peak_price - entry_price) / entry_price * 100
            else:
                mfe_pct = (entry_price - peak_price) / entry_price * 100

        for cond in conditions:
            key = f"{cond}::{exit_reason}"
            factor_exit[key]['n'] += 1
            factor_exit[key]['pnl'] += pnl
            factor_exit[key]['pnl_pct_sum'] += pnl_pct
            factor_exit[key]['holding_hours_sum'] += holding_hours
            factor_exit[key]['mfe_pcts'].append(round(mfe_pct, 2))
            if is_win:
                factor_exit[key]['wins'] += 1

            factor_summary[cond]['n'] += 1
            factor_summary[cond]['pnl'] += pnl
            factor_summary[cond]['wins'] += 1 if is_win else 0
            factor_summary[cond]['exit_breakdown'][exit_reason]['n'] += 1
            factor_summary[cond]['exit_breakdown'][exit_reason]['pnl'] += pnl
            factor_summary[cond]['exit_breakdown'][exit_reason]['wins'] += 1 if is_win else 0

        exit_summary[exit_reason]['n'] += 1
        exit_summary[exit_reason]['pnl'] += pnl
        exit_summary[exit_reason]['wins'] += 1 if is_win else 0

    # ── 格式化输出 ──
    result = {
        "generated_at": datetime.now().isoformat(),
        "trade_count": len(rows),
        "by_factor": {},
        "by_exit_reason": {},
        "by_factor_exit": [],
    }

    # 因子总览
    print(f"\n  {'Factor':<25} {'N':>3} {'WR%':>5} {'TotalPnL':>10} {'AvgPnL':>7} {'AvgMFE%':>7}")
    print("  " + "-" * 70)
    for factor, d in sorted(factor_summary.items(), key=lambda x: -x[1]['n']):
        wr = d['wins'] / d['n'] * 100 if d['n'] else 0
        avg_pnl = d['pnl'] / d['n'] if d['n'] else 0

        # avg MFE across all trades with this factor
        all_mfes = []
        for exit_r, ed in d['exit_breakdown'].items():
            for _ in range(ed['n']):
                pass  # MFE is per-trade not per-exit, we need to re-fetch
        # Simpler: collect from factor_exit
        factor_mfes = []
        for key, fd in factor_exit.items():
            if key.startswith(factor + '::'):
                factor_mfes.extend(fd['mfe_pcts'])
        avg_mfe = sum(factor_mfes) / len(factor_mfes) if factor_mfes else 0

        exits_str = " | ".join(
            f"{k}:{v['n']}pnl={v['pnl']:.0f}"
            for k, v in sorted(d['exit_breakdown'].items(), key=lambda x: -x[1]['n'])
        )
        print(f"  {factor:<25} {d['n']:>3} {wr:>4.0f}% {d['pnl']:>9.0f} {avg_pnl:>7.0f} {avg_mfe:>6.1f}%  {exits_str}")

        result["by_factor"][factor] = {
            "n": d['n'],
            "wr": round(wr / 100, 4),
            "total_pnl": round(d['pnl'], 2),
            "avg_pnl": round(avg_pnl, 2),
            "avg_mfe_pct": round(avg_mfe, 2),
            "exit_breakdown": {
                k: {"n": v['n'], "pnl": round(v['pnl'], 2), "wr": round(v['wins'] / v['n'], 4) if v['n'] else 0}
                for k, v in d['exit_breakdown'].items()
            }
        }

    # 退出方式总览
    print(f"\n  {'Exit Reason':<25} {'N':>3} {'WR%':>5} {'TotalPnL':>10} {'AvgPnL':>7}")
    print("  " + "-" * 55)
    for reason, d in sorted(exit_summary.items(), key=lambda x: -x[1]['n']):
        wr = d['wins'] / d['n'] * 100 if d['n'] else 0
        avg_pnl = d['pnl'] / d['n'] if d['n'] else 0
        print(f"  {reason:<25} {d['n']:>3} {wr:>4.0f}% {d['pnl']:>9.0f} {avg_pnl:>7.0f}")
        result["by_exit_reason"][reason] = {
            "n": d['n'],
            "wr": round(wr / 100, 4),
            "total_pnl": round(d['pnl'], 2),
            "avg_pnl": round(avg_pnl, 2),
        }

    # 因子×退出方式交叉
    print(f"\n  {'Factor::Exit':<50} {'N':>3} {'WR%':>5} {'AvgPnL':>7} {'AvgMFE%':>7}")
    print("  " + "-" * 75)
    for key, d in sorted(factor_exit.items(), key=lambda x: -x[1]['n']):
        wr = d['wins'] / d['n'] * 100 if d['n'] else 0
        avg_pnl = d['pnl'] / d['n'] if d['n'] else 0
        avg_mfe = sum(d['mfe_pcts']) / len(d['mfe_pcts']) if d['mfe_pcts'] else 0
        avg_hold = d['holding_hours_sum'] / d['n'] if d['n'] else 0
        print(f"  {key:<50} {d['n']:>3} {wr:>4.0f}% {avg_pnl:>7.0f} {avg_mfe:>6.1f}%")
        result["by_factor_exit"].append({
            "factor": key.split('::')[0],
            "exit_reason": key.split('::')[1],
            "n": d['n'],
            "wr": round(wr / 100, 4),
            "total_pnl": round(d['pnl'], 2),
            "avg_pnl": round(avg_pnl, 2),
            "avg_mfe_pct": round(avg_mfe, 2),
            "avg_holding_hours": round(avg_hold, 1),
        })

    # ── 保存 ──
    with open(OUTPUT_PATH, 'w') as f:
        json.dump(result, f, indent=2, ensure_ascii=False, default=str)
    print(f"\n  已保存到 {OUTPUT_PATH}")

    # ── 汇总 ──
    positive = {k: v for k, v in result["by_factor"].items() if v['total_pnl'] > 0}
    negative = {k: v for k, v in result["by_factor"].items() if v['total_pnl'] < 0}
    print(f"\n  ✅ 盈利因子: {list(positive.keys())}")
    print(f"  ❌ 亏损因子: {list(negative.keys())}")

    conn.close()


if __name__ == "__main__":
    main()
