#!/usr/bin/env python3
"""DEB-20261109 Batch 0.1 迁移: 把未平仓行的 exit_price=0 改成 NULL（对齐规范写入方）。

背景
----
`app/storage/trade_store.py:823`（规范写入方）早就把未平仓行的 exit_price 写成 NULL:
    `record.get("exit_price") if record.get("exit_price") else None`  # 与 exit_time IS NULL 语义对齐
只有 EXT 路径 (`scripts/extended_hours_entry_executor.py`) 仍在 INSERT 里硬编码 `exit_price=0`。
0 是"真实价格 0"的同形值 —— 不过滤 `exit_time IS NULL` 的聚合会拿到假价 0。
11-09 实测: 全库唯一一行 `exit_price=0.0 且 exit_time IS NULL` 就是 EXT 的 US.CVS (id=1828)。

本脚本: 备份 → 只改「exit_price=0 且 exit_time IS NULL」的行 → 校验 → 可重复执行 (幂等)。

用法:
  venv/bin/python scripts/migrate_ext_open_exitprice_null.py            # 只打印计划 (dry-run)
  venv/bin/python scripts/migrate_ext_open_exitprice_null.py --execute  # 执行 (自动备份)
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "ftseek_us.db"

SELECT_SQL = """
    SELECT id, trade_date, symbol, direction, entry_time, strategy_id, exit_price, exit_time
    FROM trade_records
    WHERE exit_price = 0 AND exit_time IS NULL
    ORDER BY id
"""

# 只碰「未平仓 + 价格占位 0」这一类; 已平仓行 (exit_time 非空) 一律不动。
UPDATE_SQL = """
    UPDATE trade_records SET exit_price = NULL
    WHERE exit_price = 0 AND exit_time IS NULL
"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args(argv)

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = list(con.execute(SELECT_SQL))
    print(f"待修行 {len(rows)} 行 (exit_price=0 且 exit_time IS NULL):")
    for r in rows:
        print(f"  id={r['id']} {r['trade_date']} {r['symbol']} {r['direction']} "
              f"strategy={r['strategy_id']} entry={r['entry_time']}")
    if not args.execute:
        print("\n[dry-run] 加 --execute 执行")
        return 0

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = DB.with_suffix(f".db.bak-deb20261109-b01-{ts}")
    with sqlite3.connect(bak) as b, con:
        con.backup(b)
    print(f"备份: {bak}")

    with con:
        cur = con.execute(UPDATE_SQL)
    changed = cur.rowcount

    left = con.execute(SELECT_SQL).fetchall()
    still_zero_all = con.execute(
        "SELECT COUNT(*) FROM trade_records WHERE exit_price = 0").fetchone()[0]
    open_rows = con.execute(
        "SELECT COUNT(*) FROM trade_records WHERE exit_time IS NULL").fetchone()[0]
    print(f"校验: 已改 {changed} 行 | 残留占位行 {len(left)} | 全库 exit_price=0 计数 {still_zero_all} "
          f"| 未平仓行 {open_rows}")
    ok = (len(left) == 0 and changed == len(rows))
    print("RESULT", "OK" if ok else "MISMATCH")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
