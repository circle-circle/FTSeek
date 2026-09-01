#!/usr/bin/env python3
"""data/backups 轮转 (2026-09-01 全链体检建议③, 用户裁"按建议执行").

背景: db_backup.sh 每夜 03:00 落全库快照目录 (~40M/日), 无轮转 →
体检时已积 360M (磁盘 49% 余量暂足, 但线性增长约 1.2G/月)。

策略 (只归档不删除, 回滚=解 tar):
  - 日期目录 (YYYY-MM-DD): 保留最近 14 个, 更老的 → archive/YYYY-MM-DD.tar.gz 后删原目录
  - 散落文件 (pre-*.db 等变更前备份): 超 30 天 → 合并入 archive/loose_<run_date>.tar.gz 后删原件
  - archive/ 自身不再轮转 (归档物证, 删除须人工裁决)

幂等: 归档已存在则跳过不覆盖。挂载: cron 0 8 * * 0 (周日 08:00, 周检 09:05 前)。
回滚: 删 cron 行 + 删本脚本。
"""
from __future__ import annotations

import argparse
import re
import shutil
import tarfile
from datetime import date, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BACKUPS = REPO / "data" / "backups"
ARCHIVE = BACKUPS / "archive"
KEEP_DAILY = 14
LOOSE_MAX_AGE_DAYS = 30
_DATE_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _mtime_date(p: Path) -> date:
    return datetime.fromtimestamp(p.stat().st_mtime).date()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not BACKUPS.exists():
        print("[rotate] data/backups 不存在 — 无事可做")
        return 0
    ARCHIVE.mkdir(exist_ok=True)
    today = date.today()
    archived = skipped = 0

    date_dirs = sorted(d for d in BACKUPS.iterdir() if d.is_dir() and _DATE_DIR.match(d.name))
    stale_dirs = date_dirs[:-KEEP_DAILY] if len(date_dirs) > KEEP_DAILY else []
    for d in stale_dirs:
        tgt = ARCHIVE / f"{d.name}.tar.gz"
        if tgt.exists():
            print(f"  [skip] {d.name}: 归档已存在")
            skipped += 1
            continue
        if args.dry_run:
            print(f"  [dry] {d.name} → archive/{d.name}.tar.gz")
            continue
        with tarfile.open(tgt, "w:gz") as tf:
            tf.add(d, arcname=d.name)
        shutil.rmtree(d)
        print(f"  [archived] {d.name} → {tgt.name} ({tgt.stat().st_size // 1024 // 1024}M)")
        archived += 1

    loose = [f for f in BACKUPS.iterdir()
             if f.is_file() and (today - _mtime_date(f)).days > LOOSE_MAX_AGE_DAYS]
    if loose:
        tgt = ARCHIVE / f"loose_{today.isoformat()}.tar.gz"
        if tgt.exists():
            print(f"  [skip] 散落件归档已存在: {tgt.name}")
            skipped += 1
        elif args.dry_run:
            for f in loose:
                print(f"  [dry] {f.name} → archive/{tgt.name}")
        else:
            with tarfile.open(tgt, "w:gz") as tf:
                for f in loose:
                    tf.add(f, arcname=f.name)
            for f in loose:
                f.unlink()
            print(f"  [archived] {len(loose)} 个散落件 → {tgt.name}")
            archived += 1

    total = sum(p.stat().st_size for p in BACKUPS.rglob("*") if p.is_file())
    print(f"[rotate] {'(dry) ' if args.dry_run else ''}归档 {archived} 跳过 {skipped}; "
          f"data/backups 现占 {total // 1024 // 1024}M")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
