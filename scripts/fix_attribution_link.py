#!/usr/bin/env python3
"""
修复 trade_records ↔ trade_attribution 关联断裂 (91% 断链)

根因: 系统重启后 position.trace_id 丢失, 平仓时 order_executor 用 close-xxx 兜底,
      导致 trade_records.trace_id ≠ trade_attribution.trace_id

修复策略:
  1. 按 (symbol, direction, entry_time ±15min) 模糊匹配
  2. 回填 signal_confidence, signal_strength, market_regime 到 trade_records
  3. 输出修复报告

用法: python3 scripts/fix_attribution_link.py [--dry-run]
"""

import sqlite3
import argparse
from pathlib import Path
from datetime import datetime, timedelta

DB_PATH = Path(__file__).parent.parent / "data" / "ftseek_us.db"


def find_matching_attribution(cursor, symbol, direction, entry_time_str):
    """按 symbol + direction + entry_time 模糊匹配 trade_attribution"""
    try:
        entry_dt = datetime.fromisoformat(entry_time_str)
    except (ValueError, TypeError):
        return None

    # ±15 分钟窗口
    t_start = (entry_dt - timedelta(minutes=15)).isoformat()
    t_end = (entry_dt + timedelta(minutes=15)).isoformat()

    cursor.execute("""
        SELECT trace_id, signal_strength, signal_confidence, regime,
               owner_strategy, direction
        FROM trade_attribution
        WHERE symbol = ?
          AND direction = ?
          AND entry_time >= ?
          AND entry_time <= ?
        ORDER BY ABS(julianday(entry_time) - julianday(?))
        LIMIT 1
    """, (symbol, direction, t_start, t_end, entry_time_str))

    return cursor.fetchone()


def main():
    parser = argparse.ArgumentParser(description="修复 trade_records 归因关联")
    parser.add_argument("--dry-run", action="store_true", help="仅分析不修改")
    parser.add_argument("--db", default=str(DB_PATH))
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    cursor = conn.cursor()

    # 获取所有需要修复的交易
    cursor.execute("""
        SELECT id, symbol, direction, entry_time, trace_id,
               signal_confidence, signal_strength, market_regime, realized_pnl
        FROM trade_records
        WHERE realized_pnl IS NOT NULL
        ORDER BY entry_time
    """)
    trades = cursor.fetchall()

    fixed = 0
    already_ok = 0
    no_match = 0
    results = []

    for trade in trades:
        (tr_id, symbol, direction, entry_time, tr_trace_id,
         tr_conf, tr_str, tr_regime, pnl) = trade

        # 检查是否已有有效数据
        tr_conf = tr_conf or 0
        tr_str = tr_str or 0
        tr_regime = tr_regime or ""
        if tr_conf > 0 and tr_str > 0 and tr_regime and tr_regime != "UNKNOWN":
            already_ok += 1
            continue

        # 尝试匹配 trade_attribution
        attr = find_matching_attribution(cursor, symbol, direction, entry_time)
        if attr:
            attr_trace, attr_str, attr_conf, attr_regime, attr_owner, attr_dir = attr
            results.append({
                "id": tr_id,
                "symbol": symbol,
                "direction": direction,
                "entry_time": entry_time,
                "old_trace": tr_trace_id,
                "new_trace": attr_trace,
                "new_conf": attr_conf,
                "new_str": attr_str,
                "new_regime": attr_regime,
                "pnl": pnl,
            })

            if not args.dry_run:
                # 回填 signal_confidence + signal_strength + market_regime
                updates = []
                params = []
                if attr_conf > 0 and tr_conf == 0:
                    updates.append("signal_confidence = ?")
                    params.append(attr_conf)
                if attr_str > 0 and tr_str == 0:
                    updates.append("signal_strength = ?")
                    params.append(attr_str)
                if attr_regime and (not tr_regime or tr_regime == "UNKNOWN"):
                    updates.append("market_regime = ?")
                    params.append(attr_regime)

                if updates:
                    params.append(tr_id)
                    conn.execute(
                        f"UPDATE trade_records SET {', '.join(updates)} WHERE id = ?",
                        params
                    )
                    fixed += 1
        else:
            no_match += 1

    if not args.dry_run:
        conn.commit()

    # 报告
    print(f"\n{'='*70}")
    print(f"  归因关联修复报告 {'(DRY RUN)' if args.dry_run else '(已执行)'}")
    print(f"{'='*70}")
    print(f"  总交易数:        {len(trades)}")
    print(f"  已有完整数据:    {already_ok}")
    print(f"  成功匹配:        {len(results)} ({'已修复' if not args.dry_run else '待修复'})")
    print(f"  未找到匹配:      {no_match}")
    print(f"{'='*70}")

    if results:
        print(f"\n  匹配详情 (最近 20 笔):")
        print(f"  {'标的':12} {'方向':6} {'PnL':>10} {'conf':>6} {'str':>4} {'regime':10}")
        print(f"  {'-'*55}")
        for r in results[-20:]:
            print(f"  {r['symbol']:12} {r['direction']:6} ${r['pnl']:>9.2f} "
                  f"{r['new_conf']:>6.2f} {r['new_str']:>4} {r['new_regime'] or 'N/A':10}")

    # 修复后重新统计
    if not args.dry_run:
        cursor.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN signal_confidence > 0 THEN 1 ELSE 0 END) as has_conf,
                SUM(CASE WHEN signal_strength > 0 THEN 1 ELSE 0 END) as has_str,
                SUM(CASE WHEN market_regime != 'UNKNOWN' AND market_regime != '' THEN 1 ELSE 0 END) as has_regime
            FROM trade_records WHERE realized_pnl IS NOT NULL
        """)
        row = cursor.fetchone()
        total, has_conf, has_str, has_regime = row
        print(f"\n  修复后统计:")
        print(f"    signal_confidence > 0: {has_conf}/{total} ({100*has_conf/total:.1f}%)")
        print(f"    signal_strength > 0:   {has_str}/{total} ({100*has_str/total:.1f}%)")
        print(f"    market_regime 有效:    {has_regime}/{total} ({100*has_regime/total:.1f}%)")

    conn.close()
    print(f"\n{'='*70}")


if __name__ == "__main__":
    main()
