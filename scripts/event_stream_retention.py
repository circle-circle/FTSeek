#!/usr/bin/env python3
"""event_stream.db 保留窗治理 (2026-10-15, 全链路审计 P3 落地; 用户裁「推荐方案自动清理」).

背景: 审计实测 event_stream.db 949MB / +170MB/天 / 无保留策略 — raw_events.raw_artifact
逐条存整篇正文 (GOOGLE_NEWS_RSS 均值 ~511KB/行), 而**零代码消费方读它** (消费面=
canonical_events + source_health; raw_events 只有 COUNT(*))。叠加 db_backup.sh 7 天全量
副本 ⇒ 无界增长 ×7 放大。

策略 (用户裁「14 天 + 仅清 artifact + 自动化」):
- **行全保留** (raw_id 幂等 dedup 的基座, INSERT OR IGNORE 依赖行在), 仅把
  fetched_at < 今天−14天 的行的 raw_artifact 置**空串** (列级清理)。
  ⚠️ 2026-10-17 修复: 原实现置 NULL, 而生产 schema 该列是 `TEXT NOT NULL`
  (app/event_stream/store.py) ⇒ 每轮整条 UPDATE 抛 IntegrityError, 清理从未
  生效 (cron 失败日志 0 字节, 静默失败)。置空串与置 NULL 的磁盘收益相同
  (正文已释放), 且无需重建 1.3G 表来放开约束。
- canonical_events / source_health 不动。
- `--vacuum`: VACUUM 收缩文件 (auto_vacuum=0 ⇒ 平时清出的页进 freelist 供复用,
  周频 VACUUM 才真正还盘; WAL 下需短暂无写者, busy_timeout 重试, 失败留痕下轮再来)。

用法 (cron):
  每日清理:   venv/bin/python scripts/event_stream_retention.py
  每周收缩:   venv/bin/python scripts/event_stream_retention.py --vacuum
失败纪律: 任何异常 exit 1 + 打 stderr (裸 traceback 会被 error_alert 的崩溃尾检测抓到)。
产物: 不打日志文件外的任何状态 (幂等, 重跑安全)。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DB = REPO / "data" / "event_stream.db"
RETENTION_DAYS = 14          # 用户裁「推荐方案」= 14 天窗
VACUUM_BUSY_RETRIES = 5      # 与 */5min 摄取错峰: busy 重试 5×5s, 不成则本轮放弃留痕


def _connect(rw: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB), timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def clean(days: int = RETENTION_DAYS, dry_run: bool = False) -> dict:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    # 生产 schema raw_artifact TEXT NOT NULL ⇒ 只能置空串; LENGTH>0 同时跳过空串/None
    has_body = "fetched_at < ? AND LENGTH(raw_artifact) > 0"
    with _connect() as conn:
        stats = conn.execute(
            f"SELECT COUNT(*), COALESCE(SUM(LENGTH(raw_artifact)),0) FROM raw_events "
            f"WHERE {has_body}", (cutoff,)).fetchone()
        n_hit, bytes_hit = int(stats[0]), int(stats[1])
        out = {"cutoff": cutoff, "days": days, "rows_to_null": n_hit,
               "bytes_to_free": bytes_hit, "dry_run": dry_run}
        if dry_run or n_hit == 0:
            return out
        conn.execute(
            f"UPDATE raw_events SET raw_artifact='' WHERE {has_body}", (cutoff,))
        conn.commit()
        out["nulled"] = n_hit
        return out


def vacuum() -> dict:
    """WAL 下 VACUUM 需短暂无写者; busy 重试, 失败留痕不阻塞 (下轮再来)。"""
    size_before = DB.stat().st_size
    for attempt in range(VACUUM_BUSY_RETRIES):
        try:
            conn = _connect()
            conn.execute("VACUUM")
            conn.close()
            size_after = DB.stat().st_size
            return {"vacuumed": True, "size_before": size_before,
                    "size_after": size_after,
                    "freed_mb": round((size_before - size_after) / 1048576, 1)}
        except sqlite3.OperationalError as e:
            # 连接残留由进程退出兜底 (cron 一次性进程); 不重连, 直接按忙重试
            if "locked" in str(e).lower() and attempt < VACUUM_BUSY_RETRIES - 1:
                time.sleep(5)
                continue
            return {"vacuumed": False, "reason": str(e), "size_before": size_before}
    return {"vacuumed": False, "reason": "busy_retries_exhausted",
            "size_before": size_before}


def main() -> int:
    ap = argparse.ArgumentParser(description="event_stream.db 保留窗治理")
    ap.add_argument("--days", type=int, default=RETENTION_DAYS)
    ap.add_argument("--vacuum", action="store_true", help="清理后 VACUUM 收缩文件")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    try:
        result = {"generated_at": datetime.now(timezone.utc).isoformat(),
                  "clean": clean(days=args.days, dry_run=args.dry_run)}
        if args.vacuum and not args.dry_run:
            result["vacuum"] = vacuum()
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception:
        # 裸 traceback 落 stderr ⇒ error_alert 崩溃尾检测可见 (2026-10-15 起的告警面)
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
