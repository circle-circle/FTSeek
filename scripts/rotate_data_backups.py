#!/usr/bin/env python3
"""data/backups 轮转 (2026-09-01 全链体检建议③, 用户裁"按建议执行").

背景: db_backup.sh 每夜 03:00 落全库快照目录 (~40M/日), 无轮转 →
体检时已积 360M (磁盘 49% 余量暂足, 但线性增长约 1.2G/月)。

策略 (只归档不删除, 回滚=解 tar):
  - 日期目录 (YYYY-MM-DD): 保留最近 14 个, 更老的 → archive/YYYY-MM-DD.tar.gz 后删原目录
  - 散落文件 (pre-*.db 等变更前备份): 超 30 天 → 合并入 archive/loose_<run_date>.tar.gz 后删原件
  - archive/ 自身不再轮转 (归档物证, 删除须人工裁决)

完整性闸门 (2026-09-01 复查收口): 删除原件前重开归档读全量成员校验 —
新写归档校验成员集一致+全量可读, 校验不过则撤归档保原件 (tar 写完即 rmtree 的
窗口内损坏会静默违反"不删证"); 已存在的归档只校可读性, 校验不过两样都保留
(可能含可抢救数据, 重新归档=毁证, 须人工处置)。

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


def _tar_ok(tgt: Path, expected: set[str] | None = None) -> bool:
    """重开归档读全量成员; expected 给定时校验成员集一致 (删原件前的完整性闸门)."""
    try:
        with tarfile.open(tgt, "r:gz") as tf:
            members = tf.getmembers()
            if expected is not None:
                got = {m.name for m in members if m.isfile()}
                if got != expected:
                    print(f"  [verify-fail] {tgt.name}: 成员不符 "
                          f"expect={len(expected)} got={len(got)}")
                    return False
            for m in members:
                if m.isfile():
                    fh = tf.extractfile(m)
                    if fh is None:
                        print(f"  [verify-fail] {tgt.name}: 成员 {m.name} 不可读")
                        return False
                    while fh.read(1 << 20):
                        pass
        return True
    except Exception as e:
        print(f"  [verify-fail] {tgt.name}: {e}")
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not BACKUPS.exists():
        print("[rotate] data/backups 不存在 — 无事可做")
        return 0
    ARCHIVE.mkdir(exist_ok=True)
    today = date.today()
    archived = skipped = failed = 0

    date_dirs = sorted(d for d in BACKUPS.iterdir() if d.is_dir() and _DATE_DIR.match(d.name))
    stale_dirs = date_dirs[:-KEEP_DAILY] if len(date_dirs) > KEEP_DAILY else []
    for d in stale_dirs:
        tgt = ARCHIVE / f"{d.name}.tar.gz"
        if tgt.exists():
            # 上轮已归档: 只校可读性 — 校验通过补删残留原件 (tar 落成后 rmtree 前中断
            # 的收尾); 不过则两样都保留 (归档可能含可抢救数据, 重新归档=毁证, 人工处置)
            if _tar_ok(tgt):
                if any(d.iterdir()):
                    shutil.rmtree(d)
                    print(f"  [completed] {d.name}: 归档完好, 补删残留原目录 (上轮中断收尾)")
                    archived += 1
                else:
                    print(f"  [skip] {d.name}: 归档已存在")
                    skipped += 1
            else:
                print(f"  [FAIL] {d.name}: 已存在归档校验未过 — 原目录与归档均保留, 需人工处置")
                failed += 1
            continue
        if args.dry_run:
            print(f"  [dry] {d.name} → archive/{d.name}.tar.gz")
            continue
        expected = {f"{d.name}/{f.relative_to(d).as_posix()}"
                    for f in d.rglob("*") if f.is_file()}
        with tarfile.open(tgt, "w:gz") as tf:
            tf.add(d, arcname=d.name)
        if _tar_ok(tgt, expected):
            shutil.rmtree(d)
            print(f"  [archived] {d.name} → {tgt.name} "
                  f"({tgt.stat().st_size // 1024 // 1024}M, 校验通过)")
            archived += 1
        else:
            tgt.unlink()
            print(f"  [FAIL] {d.name}: 归档校验未过, 撤归档保原目录 (下轮重试)")
            failed += 1

    loose = [f for f in BACKUPS.iterdir()
             if f.is_file() and (today - _mtime_date(f)).days > LOOSE_MAX_AGE_DAYS]
    if loose:
        tgt = ARCHIVE / f"loose_{today.isoformat()}.tar.gz"
        if tgt.exists():
            if _tar_ok(tgt):
                remain = [f for f in loose if f.exists()]
                for f in remain:
                    f.unlink()
                print(f"  [{'completed' if remain else 'skip'}] {tgt.name}: 归档完好"
                      + (f", 补删残留散落件 {len(remain)} 个" if remain else ""))
                skipped += 1
            else:
                print(f"  [FAIL] {tgt.name}: 已存在归档校验未过 — 散落件与归档均保留, 需人工处置")
                failed += 1
        elif args.dry_run:
            for f in loose:
                print(f"  [dry] {f.name} → archive/{tgt.name}")
        else:
            with tarfile.open(tgt, "w:gz") as tf:
                for f in loose:
                    tf.add(f, arcname=f.name)
            expected = {f.name for f in loose}
            if _tar_ok(tgt, expected):
                for f in loose:
                    f.unlink()
                print(f"  [archived] {len(loose)} 个散落件 → {tgt.name} (校验通过)")
                archived += 1
            else:
                tgt.unlink()
                print(f"  [FAIL] 散落件归档校验未过, 撤归档保原件 (下轮重试)")
                failed += 1

    total = sum(p.stat().st_size for p in BACKUPS.rglob("*") if p.is_file())
    print(f"[rotate] {'(dry) ' if args.dry_run else ''}归档 {archived} 跳过 {skipped} "
          f"失败 {failed}; data/backups 现占 {total // 1024 // 1024}M")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
