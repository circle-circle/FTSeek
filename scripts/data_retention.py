#!/usr/bin/env python3
"""data/ 无界增长治理 v1 — replay_cache 按龄保留 (2026-10-08 运维体检批).

背景: data/ 约 2.7GB, 其中 replay_cache 约 1.1GB (1500+ 个按日期区间命名的
K 线缓存文件), 不在任何 retention 内; disk_cleanup.sh 只管日志/备份/journal。

v1 只清**可再生缓存**: replay_cache 的消费方 (scripts/replay_ext_entry_sources.py
的 CACHE_DIR 读取分支) 在文件缺失时会自动重新拉取并回写 — 删除的代价是
网络重取, 不是数据损失。判据 = 文件 mtime 超过 N 天 (默认 30;
2026-10-18 减法批 90→30: 最后写入 9-22 的 1.1GB 可再生缓存无需再等 90 天)。

刻意不清 (v1 边界, 扩面需先审计消费方):
  - data/replay_bars*/ : 30+ 个回放/反事实脚本的**历史输入** (按日文件,
    缺失会导致历史重放失真, 非缓存语义);
  - data/*.jsonl       : 多个 eval 脚本读全文件做历史结算; 其中
    futu_capital_flow.jsonl 的活跃消费方已改尾部增量读
    (app/playbook/intraday_reentry.py), 其余 jsonl 未逐个审计。

用法: 默认 dry-run; --apply 才真删。挂载: crontab 04:20 (在 03:00 DB 备份、
04:00 全局磁盘清理之后)。回滚 = 删 crontab 行 + 本脚本 (纯删除型运维,
零交易行为, 不触任何退出参数)。
"""
from __future__ import annotations

import argparse
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "data" / "replay_cache"
DEFAULT_DAYS = 30


def collect_stale(cache_dir: Path, days: int, now: float | None = None) -> list[Path]:
    """返回 mtime 早于 (now - days*86400) 的 replay_cache json 文件.

    目录缺失 → 空列表 (首跑/新环境不炸); 只认顶层 *.json, 不递归 —
    未来若出现子目录 (如按日期分桶), 由扩面批显式接管, 不静默深扫。
    """
    if days < 0:
        raise ValueError(f"days 必须 >= 0, 收到 {days}")
    if not cache_dir.is_dir():
        return []
    cutoff = (now if now is not None else time.time()) - days * 86400.0
    return [
        path for path in sorted(cache_dir.glob("*.json"))
        if path.is_file() and path.stat().st_mtime < cutoff
    ]


def _size_bytes(paths: list[Path]) -> int:
    return sum(path.stat().st_size for path in paths)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="data/replay_cache 按龄保留 (默认 dry-run)")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                        help=f"保留天数, 按 mtime 判定 (默认 {DEFAULT_DAYS})")
    parser.add_argument("--apply", action="store_true",
                        help="真删 (缺省仅列出将删文件与字节数)")
    parser.add_argument("--cache-dir", default=str(CACHE_DIR),
                        help="缓存目录 (默认 data/replay_cache; 测试用)")
    args = parser.parse_args(argv)

    cache_dir = Path(args.cache_dir)
    stale = collect_stale(cache_dir, args.days)
    stale_bytes = _size_bytes(stale)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{stamp}] data_retention {mode}: dir={cache_dir} days={args.days} "
          f"stale_files={len(stale)} stale_bytes={stale_bytes}")
    for path in stale:
        print(f"  {'DEL' if args.apply else 'WOULD-DEL'} {path.name} "
              f"({path.stat().st_size} bytes, mtime={datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec='seconds')})")
    if args.apply:
        _n_vanished = 0
        for path in stale:
            try:
                path.unlink()
            except FileNotFoundError:
                # R5 留痕 (2026-10-09 复查清剿): 扫描→删除窗口内文件被并发清走,
                #   计数进汇总行, 不让「全删成」与「有竞态」输出同形。
                _n_vanished += 1
        print(f"[{stamp}] 已删除 {len(stale) - _n_vanished} 个过期缓存文件 "
              f"({len(stale)} 文件 / {stale_bytes} bytes 计划量"
              f"{f', 扫描后被并发清走 {_n_vanished}' if _n_vanished else ''})")
    elif stale:
        print(f"[{stamp}] dry-run 未删除; 加 --apply 执行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
