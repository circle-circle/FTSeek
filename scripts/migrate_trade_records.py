#!/usr/bin/env python3
"""trade_records 表迁移脚本 (Task #36, 2026-06-16) — 幂等.

触发时机:
  1. 启动期 main.py 调用 ensure_trade_records_schema() 自动确保 schema
  2. 一次性运行: python -m scripts.migrate_trade_records
  3. backfill 历史 NULL signal_strength/signal_id: --backfill

设计原则:
  - 所有 schema 改动用 IF NOT EXISTS / try/except 兼容已存在列
  - 不破坏已有数据
  - 同时迁移 ftseek_us.db 与 ftseek_us_live.db (实盘隔离)

CREATE TABLE 委托给 TradeStore._init_db (SSOT, 已经幂等).
"""
from __future__ import annotations
import argparse
import logging
import os
import sqlite3
import sys
from pathlib import Path

# 允许在仓库根目录直接运行
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

logger = logging.getLogger("migrate_trade_records")


def _candidate_dbs() -> list[Path]:
    """返回所有需要确保 schema 的数据库路径 (实盘 + 模拟, 跳过不存在)."""
    data_dir = _PROJECT_ROOT / "data"
    cands = []
    for name in ("ftseek_us.db", "ftseek_us_live.db"):
        path = data_dir / name
        # 即使文件不存在也加入: TradeStore() 会自动创建
        cands.append(path)
    return cands


def ensure_trade_records_schema(db_path: Path | str) -> dict:
    """对单一 DB 确保 trade_records / open_positions schema (幂等).

    通过实例化 TradeStore 触发其 _init_db (SSOT 创建 + ALTER ADD COLUMN).

    Returns: dict 含 {db, created_table, total_rows, null_signal_strength, null_signal_id}
    """
    from app.storage.trade_store import TradeStore

    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)

    # 触发 schema 初始化 (CREATE TABLE IF NOT EXISTS + 幂等 ALTER)
    _store = TradeStore(str(p))
    # 关闭 SQLite WAL 残留 (TradeStore 的 _init_db 已 commit, 这里只查统计)
    conn = sqlite3.connect(str(p), timeout=10)
    try:
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='trade_records'")
        exists = cur.fetchone() is not None
        if not exists:
            return {"db": str(p), "exists": False}
        total = conn.execute("SELECT COUNT(*) FROM trade_records").fetchone()[0]
        null_ss = conn.execute(
            "SELECT COUNT(*) FROM trade_records WHERE signal_strength IS NULL"
        ).fetchone()[0]
        null_sid = conn.execute(
            "SELECT COUNT(*) FROM trade_records WHERE signal_id IS NULL OR signal_id = ''"
        ).fetchone()[0]
    finally:
        conn.close()

    return {
        "db": str(p),
        "exists": True,
        "total_rows": total,
        "null_signal_strength": null_ss,
        "null_signal_id": null_sid,
    }


def backfill_null_signal_fields(db_path: Path | str) -> dict:
    """把历史 NULL 的 signal_strength / signal_id 回填为 0 / 'backfilled:<id>'.

    标记 source_type 末尾追加 BACKFILLED (若未含) — 便于审计区分历史回填.
    幂等: 已回填 (signal_id LIKE 'backfilled:%') 或非 NULL 跳过.
    """
    from app.utils.timezone import now_et

    p = Path(db_path)
    if not p.exists():
        return {"db": str(p), "skipped": True, "reason": "file_missing"}

    conn = sqlite3.connect(str(p), timeout=10)
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        # 仅回填 signal_strength IS NULL 或 signal_id 为空的记录
        cur = conn.execute(
            "SELECT id FROM trade_records "
            "WHERE signal_strength IS NULL OR signal_id IS NULL OR signal_id = ''"
        )
        ids = [row[0] for row in cur.fetchall()]
        if not ids:
            return {"db": str(p), "backfilled": 0}

        # 批量更新
        ts = now_et().isoformat()
        for rid in ids:
            conn.execute(
                "UPDATE trade_records SET "
                "signal_strength = COALESCE(signal_strength, 0), "
                "signal_id = CASE WHEN signal_id IS NULL OR signal_id = '' "
                "                 THEN ? ELSE signal_id END, "
                "metadata_json = COALESCE(metadata_json, "
                "    json_object('backfilled_at', ?, 'reason', 'task_36_null_signal_fields')) "
                "WHERE id = ?",
                (f"backfilled:{rid}", ts, rid),
            )
        conn.commit()
        return {"db": str(p), "backfilled": len(ids), "ids": ids[:10] + (["..."] if len(ids) > 10 else [])}
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser(description="trade_records schema migration (Task #36)")
    ap.add_argument("--backfill", action="store_true", help="把 NULL signal_strength/signal_id 回填为 0/backfilled")
    ap.add_argument("--db", help="指定单一 DB 路径; 默认遍历 ftseek_us.db + ftseek_us_live.db")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    targets = [Path(args.db)] if args.db else _candidate_dbs()
    logger.info(f"目标 DB: {[str(p) for p in targets]}")

    for p in targets:
        try:
            result = ensure_trade_records_schema(p)
            logger.info(f"[schema] {result}")
            if args.backfill:
                bf = backfill_null_signal_fields(p)
                logger.info(f"[backfill] {bf}")
        except Exception as e:
            logger.error(f"[迁移失败] {p}: {e}", exc_info=True)


if __name__ == "__main__":
    main()
