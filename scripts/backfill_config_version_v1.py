#!/usr/bin/env python3
"""F7 数据修复: 把落 schema 默认 'v1' 的 7 行显式版本回填为 'v2' (2026-09-18 用户令「修」).

机制: trade_records.config_version 的 schema 默认是 'v1', 规范写入方 app/storage/trade_store.py
显式写 'v2'; 延长时段开仓写方(scripts/extended_hours_entry_executor.py) 与持仓同步写方
(app/core/position_sync.py) 此前**漏写本列** ⇒ 落 'v1' ⇒ W1 冻结窗版本分裂假象。
两个写方已修; 本脚本回填已产生的 7 行 (值 = 同期实际生效版本 'v2', 与全窗其余 186 行一致)。

安全: 默认 dry-run; --apply 前做 SQLite 在线 backup(); 只动 config_version='v1' 且判定集大小==EXPECT 的行,
大小不符即中止 (防误伤)。
"""
import argparse
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

DB = Path("/opt/ftseek/FTSeek-US/data/ftseek_us.db")
EXPECT = 7


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB), timeout=30)
    try:
        # 2026-09-18 修正: 'v1' 全库有 359 行 —— 旧行(6-7 月 MAIN_PIPELINE)的 'v1' 可能是**真实历史标签**
        # (v1→v2 换版), 不是 schema 默认产物 ⇒ 回填**只限冻结窗内**(窗口内同期其余行全为 'v2', 且写方
        # 就是漏写该列的这两处 POSITION_SYNC / extended_hours_entry) ⇒ 改动范围 = 7 行, 不碰历史。
        rows = conn.execute(
            "SELECT id, symbol, source_type, strategy_id, substr(entry_time,1,10) "
            "FROM trade_records WHERE config_version='v1' "
            "AND COALESCE(exit_time, entry_time) >= '2026-08-24' ORDER BY id"
        ).fetchall()
        print(f"[dry] config_version='v1' 命中 {len(rows)} 行 (expect {EXPECT})")
        for r in rows:
            print("   ", r)
        dist = dict(conn.execute("SELECT config_version, COUNT(*) FROM trade_records GROUP BY 1").fetchall())
        print("[dry] 回填前版本分布:", dist)
        if len(rows) != EXPECT:
            print(f"⛔ 命中数 {len(rows)} != {EXPECT} ⇒ 中止(防误伤; 先人工核)")
            return 2
        if not args.apply:
            print("[dry] 未 --apply, 无写入")
            return 0

        bk = DB.parent / "backups" / f"ftseek_us_before_config_version_fix_{datetime.now():%Y%m%d_%H%M%S}.db"
        bk.parent.mkdir(parents=True, exist_ok=True)
        dst = sqlite3.connect(str(bk))
        with dst:
            conn.backup(dst)
        dst.close()
        print(f"[apply] 备份 → {bk} ({bk.stat().st_size/1e6:.1f} MB)")

        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "UPDATE trade_records SET config_version='v2' WHERE config_version='v1' "
            "AND COALESCE(exit_time, entry_time) >= '2026-08-24'"
        )
        conn.commit()
        print(f"[apply] 更新 {cur.rowcount} 行 → 'v2'")
        print("[apply] 回填后版本分布:",
              dict(conn.execute("SELECT config_version, COUNT(*) FROM trade_records GROUP BY 1").fetchall()))
        print("[apply] 冻结窗内 'v1' 残留:",
              conn.execute("SELECT COUNT(*) FROM trade_records WHERE config_version='v1' "
                           "AND COALESCE(exit_time, entry_time) >= '2026-08-24'").fetchone()[0])
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())