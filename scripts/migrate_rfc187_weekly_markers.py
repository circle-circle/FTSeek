#!/usr/bin/env python3
"""RFC-187 迁移: 把 playbook_regime_log 里的周报幂等标记行搬到 playbook_weekly_markers。

背景: 旧写法 `regime='weekly_report'` + `date=周报周五` 写进环境表, 撞 UNIQUE(date),
      与当日环境行互斥 (谁后写谁赢) ⇒ 实测窗口 18 个周五只有 2 天有环境行。
本脚本: 备份 → 建新表 → 搬 12 行 → 删旧行 → 校验。

用法:
  venv/bin/python3.11 scripts/migrate_rfc187_weekly_markers.py            # 只打印计划 (dry-run)
  venv/bin/python3.11 scripts/migrate_rfc187_weekly_markers.py --execute  # 执行 (自动备份)
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "ftseek_us.db"

DDL = """CREATE TABLE IF NOT EXISTS playbook_weekly_markers (
    week_end TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
)"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args(argv)

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    markers = list(con.execute(
        "SELECT date, notes, created_at FROM playbook_regime_log WHERE regime LIKE '%weekly%' ORDER BY date"))
    env_rows = con.execute(
        "SELECT COUNT(*) FROM playbook_regime_log WHERE regime NOT LIKE '%weekly%'").fetchone()[0]
    print(f"待搬标记 {len(markers)} 行 / 环境行保留 {env_rows} 行")
    for m in markers:
        print(f"  {m['date']}  {m['created_at']}")

    if not args.execute:
        print("\n[dry-run] 加 --execute 执行")
        return 0

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = DB.with_suffix(f".db.bak-rfc187-{ts}")
    with sqlite3.connect(bak) as b, con:
        con.backup(b)
    print(f"备份: {bak}")

    con.execute(DDL)
    with con:
        for m in markers:
            con.execute(
                "INSERT INTO playbook_weekly_markers (week_end, created_at) VALUES (?, ?) "
                "ON CONFLICT(week_end) DO UPDATE SET created_at=excluded.created_at",
                (m["date"], m["created_at"]))
        con.execute("DELETE FROM playbook_regime_log WHERE regime LIKE '%weekly%'")

    left = con.execute(
        "SELECT COUNT(*) FROM playbook_regime_log WHERE regime LIKE '%weekly%'").fetchone()[0]
    new_n = con.execute("SELECT COUNT(*) FROM playbook_weekly_markers").fetchone()[0]
    env_after = con.execute(
        "SELECT COUNT(*) FROM playbook_regime_log WHERE regime NOT LIKE '%weekly%'").fetchone()[0]
    print(f"校验: 旧标记残留={left} 新表={new_n} 环境行 {env_rows}->{env_after}")
    ok = (left == 0 and new_n == len(markers) and env_after == env_rows)
    print("RESULT", "OK" if ok else "MISMATCH")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())