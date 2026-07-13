#!/usr/bin/env python3
"""
一次性数据卫生迁移: 清理历史坏符号 (写入侧已在 symbol_utils 修根因)

修复两类:
  1. 双前缀   US.US.MU -> US.MU
  2. 别名割裂 US.GOOGL -> US.GOOG

安全措施:
  - 迁移前自动备份每个 DB (.bak_symhygiene_<ts>)
  - 碰撞处理: 若目标正名已在同 key 存在 (如 playbook_instructions UNIQUE(date,symbol)),
    删除坏变体行、保留正名行; 否则直接改名
  - 全程打印 before/after 审计

用法:
  PYTHONPATH=. ./venv/bin/python scripts/fix_symbol_hygiene.py [--apply]
  (不带 --apply 为 dry-run, 只报告不改)
"""
import os
import sys
import shutil
import sqlite3
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.utils.symbol_utils import normalize_symbol  # noqa: E402

DBS = [
    "data/ftseek_us.db",
    "data/ftseek_us_live.db",
    "data/decisions.db",
    "data/signal_snapshots.db",
    "data/strategy_memory.db",
    "data/theme_radar_history.db",
]
SYM_COLS = ("symbol", "code", "ticker", "stock_code")
TS = datetime.now().strftime("%Y%m%d_%H%M%S")


def _bad_filter(col: str) -> str:
    return f"({col} LIKE 'US.US.%' OR {col} = 'US.GOOGL' OR {col} = 'GOOGL')"


def _tables(conn):
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]


def _cols(conn, t):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]


def migrate_db(path: str, apply: bool):
    if not os.path.exists(path):
        print(f"  (跳过, 不存在) {path}")
        return
    print(f"\n=== {path} ===")
    if apply:
        bak = f"{path}.bak_symhygiene_{TS}"
        shutil.copy2(path, bak)
        print(f"  备份: {bak}")
    conn = sqlite3.connect(path)
    total_renamed = total_deleted = 0
    for t in _tables(conn):
        cols = _cols(conn, t)
        symcol = next((c for c in SYM_COLS if c in cols), None)
        if not symcol:
            continue
        has_date = "date" in cols
        bads = [r[0] for r in conn.execute(
            f"SELECT DISTINCT {symcol} FROM {t} WHERE {_bad_filter(symcol)}")]
        for bad in bads:
            good = normalize_symbol(bad)
            if good == bad:
                continue
            n = conn.execute(
                f"SELECT COUNT(*) FROM {t} WHERE {symcol}=?", (bad,)).fetchone()[0]
            if not apply:
                print(f"  [dry] {t}.{symcol}: {bad} -> {good}  ({n} 行)")
                continue
            try:
                cur = conn.execute(
                    f"UPDATE {t} SET {symcol}=? WHERE {symcol}=?", (good, bad))
                conn.commit()
                total_renamed += cur.rowcount
                print(f"  改名 {t}.{symcol}: {bad} -> {good}  ({cur.rowcount} 行)")
            except sqlite3.IntegrityError:
                conn.rollback()
                # 碰撞: 目标正名已存在 → 删坏变体(同 date 冲突的), 再改剩余
                if has_date:
                    d = conn.execute(
                        f"DELETE FROM {t} WHERE {symcol}=? AND date IN "
                        f"(SELECT date FROM {t} WHERE {symcol}=?)", (bad, good))
                    deleted = d.rowcount
                    cur = conn.execute(
                        f"UPDATE {t} SET {symcol}=? WHERE {symcol}=?", (good, bad))
                    conn.commit()
                    total_deleted += deleted
                    total_renamed += cur.rowcount
                    print(f"  碰撞 {t}.{symcol}: {bad} -> {good}  "
                          f"(删变体 {deleted}, 改名 {cur.rowcount})")
                else:
                    d = conn.execute(
                        f"DELETE FROM {t} WHERE {symcol}=?", (bad,))
                    conn.commit()
                    total_deleted += d.rowcount
                    print(f"  碰撞(无date, 删变体) {t}.{symcol}: {bad}  ({d.rowcount} 行)")
    conn.close()
    print(f"  小计: 改名 {total_renamed} 行, 删变体 {total_deleted} 行")
    return total_renamed, total_deleted


def main():
    apply = "--apply" in sys.argv
    print("模式:", "APPLY(写库)" if apply else "DRY-RUN(只报告)")
    for db in DBS:
        migrate_db(db, apply)
    if not apply:
        print("\n(dry-run 完成; 加 --apply 执行)")


if __name__ == "__main__":
    main()
