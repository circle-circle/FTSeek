#!/usr/bin/env python3
"""P0' 测量 (2026-07-21 标的量辩论裁决): VWAP 门拦截 dev 分布 + 被拦标的后续转化。

只读: 解析日志中 "VWAP门拦截: ... dev=X dir=Y" 行, 按 |dev| 分档统计,
并与 trade_records 交叉区分 "延迟后成交" vs "当日未成交".
用途: 裁决 VWAP 容差 0.3% 是否需要讨论 (若拦截集中在 0.3~0.45% 才有放宽资格).
用法: venv/bin/python scripts/measure_vwap_gate_dist.py [日志文件...]
"""
import collections
import gzip
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAT = re.compile(
    r"VWAP门拦截: (US\.[A-Z0-9]+) price=\$([\d.]+) vwap=\$([\d.]+) dev=(-?[\d.]+) dir=(\w+)")

# 日志按北京时间轮转: 美股 ET 交易日 D 的盘中落在北京 D 21:30 ~ D+1 04:00,
# 即主要在 log-(D+1) 文件中. 默认测最近两个低量交易日.
DEFAULT_DAYS = {
    "2026-07-17": ROOT / "logs/ftseek_us.log-20260718.gz",
    "2026-07-20": ROOT / "logs/ftseek_us.log-20260721",
}


def read_lines(p: Path):
    if not p.exists():
        print(f"[skip] {p} 不存在")
        return
    fh = gzip.open(p, "rt", errors="replace") if p.suffix == ".gz" else open(p, errors="replace")
    with fh:
        yield from fh


def band(d: float) -> str:
    if d < 0.005:
        return "0.3~0.5%"
    if d < 0.008:
        return "0.5~0.8%"
    return ">0.8%"


def main() -> None:
    days = DEFAULT_DAYS
    if len(sys.argv) > 1:  # 自定义: 日期=文件路径 对
        days = {}
        for spec in sys.argv[1:]:
            d, _, fp = spec.partition("=")
            days[d] = Path(fp)

    db = sqlite3.connect(str(ROOT / "data/ftseek_us.db"))
    for day, path in days.items():
        bands = collections.Counter()
        dirs = collections.Counter()
        band_syms = collections.defaultdict(set)
        blocked = set()
        for ln in read_lines(path):
            m = PAT.search(ln)
            if not m:
                continue
            sym, dev, d = m.group(1), abs(float(m.group(4))), m.group(5)
            blocked.add(sym)
            bands[band(dev)] += 1
            dirs[d] += 1
            band_syms[band(dev)].add(sym)
        traded = {r[0] for r in db.execute(
            "SELECT DISTINCT symbol FROM trade_records WHERE entry_time LIKE ?", (day + "%",))}
        print(f"\n== {day} ({path.name}) ==")
        print(f"拦截总次数={sum(bands.values())} 去重标的={len(blocked)} 方向={dict(dirs)}")
        for b in ("0.3~0.5%", "0.5~0.8%", ">0.8%"):
            ss = sorted(band_syms.get(b, ()))
            print(f"  {b}: {bands.get(b, 0)} 次 / {len(ss)} 标的 {ss[:10]}")
        delayed = sorted(blocked & traded)
        killed = sorted(blocked - traded)
        print(f"  延迟后成交={len(delayed)} {delayed}")
        print(f"  当日未成交={len(killed)} {killed}")


if __name__ == "__main__":
    main()
