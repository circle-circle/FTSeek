#!/usr/bin/env python3
"""入场时段队列监控 (2026-08-21, 8/20复盘行动项落地)。

背景: 8/15 禁触窗 1015→0935 (DEB§33 用户裁"与实盘标准看齐") 后, 09:35-10:00
重新成为入场时段。8/14-20 窗口复算: 该时段 35笔 净-$525, 但拆开看 8/18 单日
-$868 扛下几乎全部 (8/19 +$115 / 8/20 +$237) — 高方差日驱动还是稳定负期望未定,
不回滚不禁触, 继续按日分列追踪至裁决。

用法: PYTHONPATH=. venv/bin/python scripts/entry_window_cohort.py [--days 10]
"""
import argparse
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from app.core.db_utils import CURRENT_ERA_START, era_clean_where  # noqa: E402

DB = ROOT / "data" / "ftseek_us.db"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10)
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    # A10 现役时代口径 (2026-10-14 用户裁): 表现类读数剔 4-6 月 1M 资金旧时代,
    # 防 --days 放宽后旧时代大单/人工单倒灌 (SSOT: app/core/db_utils)。
    era = era_clean_where(conn)
    rows = conn.execute(
        "SELECT trade_date, "
        "  SUM(CASE WHEN substr(entry_time,12,5)>='09:35' AND substr(entry_time,12,5)<'10:00' "
        "      THEN pnl_net ELSE 0 END) AS win_pnl, "
        "  SUM(CASE WHEN substr(entry_time,12,5)>='09:35' AND substr(entry_time,12,5)<'10:00' "
        "      THEN 1 ELSE 0 END) AS win_n, "
        "  ROUND(SUM(pnl_net),1) AS day_pnl, COUNT(*) AS day_n "
        "FROM trade_records "
        "WHERE trade_date >= ? AND exit_reason IS NOT NULL AND exit_reason != '' "
        + era + " "
        "GROUP BY trade_date ORDER BY trade_date",
        ((datetime.utcnow() - timedelta(days=args.days)).strftime("%Y-%m-%d"),)).fetchall()
    conn.close()

    print("入场时段队列 09:35-10:00 (8/15 禁触窗 1015→0935 后):")
    print(f"  [口径: 现役时代 >={CURRENT_ERA_START} + data_quality clean/real, "
          f"剔 POSITION_SYNC/MANUAL (判据 A10)]")
    tot_n, tot_pnl = 0, 0.0
    for r in rows:
        if r["win_n"]:
            tot_n += r["win_n"]
            tot_pnl += r["win_pnl"] or 0
        print(f"  {r['trade_date']}: 该时段 n={r['win_n'] or 0} 净{(r['win_pnl'] or 0):+.0f}"
              f"  | 全日 n={r['day_n']} 净{r['day_pnl']:+.0f}")
    print(f"  合计: n={tot_n} 净{tot_pnl:+.0f}")


if __name__ == "__main__":
    main()
