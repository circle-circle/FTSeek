#!/usr/bin/env python3
"""去重 MAIN_PIPELINE 误标记的 playbook 平仓记录 (dry-run 优先).

背景 (2026-07-14):
  playbook 仓位若被旧主管线退出路径平仓 (protection=managed → _close_position),
  会在 trade_records 生成一条 source_type='MAIN_PIPELINE' 但 owner_strategy='playbook_sim'
  的重复记录, 与真正的 PLAYBOOK_SIM 记录并存。
  → 通知误标 "主线策略 (LLM投票)" + 归因双计数。

去重判据 (精确, 不误伤合法主线记录):
  source_type='MAIN_PIPELINE'
  AND owner_strategy LIKE 'playbook%'
  AND 同 symbol+trade_date 存在 PLAYBOOK_SIM/LIVE 记录 (真源)

用法:
  python scripts/dedup_playbook_main_dupes.py            # dry-run, 只报告
  python scripts/dedup_playbook_main_dupes.py --apply    # 备份 DB 后物理删除

安全:
  - dry-run 为默认, 不写库。
  - --apply 前自动 .bak 备份整个 DB 文件。
  - 只 DELETE 命中判据的行, 逐条打印被删 id/symbol/pnl。
"""
import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "ftseek_us.db"

DUPE_QUERY = """
    SELECT m.id, m.trade_date, m.symbol, m.owner_strategy, m.exit_reason,
           ROUND(m.realized_pnl, 2) AS pnl, m.close_futu_order_id,
           (SELECT GROUP_CONCAT(p.id) FROM trade_records p
            WHERE p.symbol = m.symbol AND p.trade_date = m.trade_date
              AND p.source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE')) AS pb_ids
    FROM trade_records m
    WHERE m.source_type = 'MAIN_PIPELINE'
      AND m.owner_strategy LIKE 'playbook%'
    ORDER BY m.trade_date DESC, m.symbol
"""


def find_dupes(conn):
    rows = conn.execute(DUPE_QUERY).fetchall()
    # 仅保留确有 PLAYBOOK 真源兄弟记录的行 (防止孤儿误删)
    return [r for r in rows if r[7]]  # r[7] = pb_ids not null


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="实际执行删除 (默认 dry-run)")
    ap.add_argument("--db", default=str(DB_PATH), help="DB 路径")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"[ERROR] DB 不存在: {db}")
        return 1

    conn = sqlite3.connect(str(db), timeout=10)
    dupes = find_dupes(conn)

    if not dupes:
        print("[OK] 无误标记的 MAIN_PIPELINE×playbook 重复记录, 无需处理。")
        conn.close()
        return 0

    print(f"{'='*72}")
    print(f"发现 {len(dupes)} 条误标记 MAIN_PIPELINE×playbook 重复记录 "
          f"({'APPLY 模式' if args.apply else 'DRY-RUN 只报告'}):")
    print(f"{'='*72}")
    for r in dupes:
        _id, _date, _sym, _owner, _reason, _pnl, _close_oid, _pb_ids = r
        print(f"  [删] id={_id} {_date} {_sym:10s} owner={_owner} "
              f"exit={_reason} pnl={_pnl} close_oid={_close_oid}")
        print(f"       └ 保留真源 PLAYBOOK 记录 id={_pb_ids}")

    if not args.apply:
        print(f"\n[DRY-RUN] 未写库。确认无误后加 --apply 执行删除。")
        conn.close()
        return 0

    # ── APPLY: 备份后删除 ──
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = db.with_suffix(db.suffix + f".bak_dedup_{ts}")
    shutil.copy2(db, bak)
    print(f"\n[备份] {bak}")

    ids = [r[0] for r in dupes]
    placeholders = ",".join("?" * len(ids))
    cur = conn.execute(
        f"DELETE FROM trade_records WHERE id IN ({placeholders})", ids
    )
    conn.commit()
    conn.close()
    print(f"[完成] 已删除 {cur.rowcount} 条重复记录。备份: {bak}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
