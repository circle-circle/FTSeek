#!/usr/bin/env python3.11
"""开盘/盘后健康速判 (只读) — 2026-11-03 开盘卡顿批配套核验器.

一眼回答四件事, 全部有日志行做依据:
  1. 今天有没有进程级冻结 / 被 SIGKILL / 重启过几次;
  2. GC 停顿有多长 (窗口最长 / 启动以来最长), 堆峰值多大;
  3. 最慢的几个同步步骤是谁 ([轮次计时] 长尾), 以及它们是否是"执行前"步骤;
  4. 入场执行是否在跑 (auto_execute 次数/耗时, 下单受理数).

用法:
  PYTHONPATH=. venv/bin/python scripts/open_health_report.py                 # 今天
  PYTHONPATH=. venv/bin/python scripts/open_health_report.py --date 2026-11-02
  PYTHONPATH=. venv/bin/python scripts/open_health_report.py --push          # 走钉钉/飞书

只读: 不连 OpenD、不写库、不改任何状态。
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LOGS = REPO / "logs"


def _log_files_for(day: str):
    """当天日志候选 (轮转感知): 当前文件 + `-<day>` + `-<day+1>` (轮转发生在次日凌晨,
    前一天的尾段落在次日命名的文件里 — 只按当天命名找会漏掉整个交易时段)。"""
    from datetime import timedelta
    out = []
    cur = LOGS / "ftseek_us.log"
    if cur.exists():
        out.append(cur)
    d = datetime.strptime(day, "%Y-%m-%d").date()
    for cand_day in (d, d + timedelta(days=1)):
        base = LOGS / ("ftseek_us.log-" + cand_day.strftime("%Y%m%d"))
        for p in (base, Path(str(base) + ".gz")):
            if p.exists() and p not in out:
                out.append(p)
    return out


def _iter_lines(paths, day: str):
    for p in paths:
        fh = None
        try:
            if p.suffix == ".gz":
                fh = gzip.open(p, "rt", encoding="utf-8", errors="replace")
            else:
                fh = open(p, encoding="utf-8", errors="replace")
        except Exception as _open_err:
            print(f"[warn] 打不开 {p} (跳过): {_open_err}", file=sys.stderr)
        if fh is None:
            continue
        with fh:
            for line in fh:
                if line.startswith(day):
                    yield line.rstrip("\n")


RE_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3})")


def _gc_summary(lines):
    """返回 (窗口最长停顿 ms 列表, 启动以来最长 ms, gen2 次数, gen 统计, RSS 序列)。"""
    windows, since, gen2, gc_stats, rss = [], 0.0, 0, None, []
    for ln in lines:
        if "[GC] 10min 汇总" in ln:
            m = re.search(r"gen0=(\d+)次/(\d+)回收 gen1=(\d+)次/(\d+)回收 gen2=(\d+)次/(\d+)回收", ln)
            if m:
                gc_stats = tuple(int(x) for x in m.groups())
            m2 = re.search(r"本窗口停顿 (\d+) 次, 最长 (\d+)ms", ln)
            if m2:
                windows.append((int(m2.group(1)), int(m2.group(2))))
            m3 = re.search(r"启动以来最长 (\d+)ms", ln)
            if m3:
                since = max(since, int(m3.group(1)))
            m4 = re.search(r"RSS=(\d+)MB", ln)
            if m4:
                rss.append(int(m4.group(1)))
        elif "[GC] 停顿" in ln:
            m = re.search(r"停顿 (\d+)ms ≥ \d+ms \(gen=(\d+)", ln)
            if m:
                windows.append((1, int(m.group(1))))
                since = max(since, int(m.group(1)))
    gen2 = sum(1 for ln in lines if "gen2 全堆扫描开始" in ln)
    return windows, since, gen2, gc_stats, rss


def _phase_stats(lines):
    """[轮次计时] 长尾: label → [耗时...] + RSS 台阶行数。"""
    ph = defaultdict(list)
    rss_steps = Counter()
    for ln in lines:
        if "[轮次计时]" not in ln:
            continue
        m = re.search(r"\[轮次计时\] (\S+) ([\d.]+)s", ln)
        if not m:
            continue
        ph[m.group(1)].append(float(m.group(2)))
        if "RSS " in ln:
            rss_steps[m.group(1)] += 1
    return ph, rss_steps


def _freeze_events(day: str):
    """冻结/重启: sentinel 日志 + journal + 栈文件。"""
    sent = []
    p = LOGS / "freeze_sentinel.log"
    if p.exists():
        for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
            if not ln.startswith("[" + day.replace("-", "") + "_"):
                continue
            # "liveness skip: transitioning" 是运维重启时的让路记录, 不是冻结处置
            if "liveness skip" in ln:
                continue
            if any(k in ln for k in ("快速层", "兜底阈值", "冻结", "SIGKILL", "revive", "存活层")):
                sent.append(ln)
    stacks = sorted(LOGS.glob("freeze_stacks/freeze_%s*.txt" % day.replace("-", "")))
    kills = 0
    try:
        out = subprocess.run(
            ["journalctl", "-u", "ftseek-us", "--since", day + " 00:00:00",
             "--until", day + " 23:59:59", "--no-pager"],
            capture_output=True, text=True, timeout=60,
        ).stdout
        kills = out.count("status=9/KILL") + out.count("status=11/SEGV")
    except Exception as _j_err:
        print(f"[warn] journalctl 读取失败 (冻结计数可能不准): {_j_err}", file=sys.stderr)
    return sent, [s.name for s in stacks], kills


def _exec_stats(lines, max_pair_s: float = 300.0):
    """auto_execute: 轮次数、单轮耗时、下单受理数、报错、跨重启/未配对计数。

    配对纪律: `开始` → 下一条 `完成` 才算一轮; 间隔 > max_pair_s 视为跨重启/被冻结打断的
    伪配对 (11-02 那轮被 SIGKILL 打断, 直接配会报出 212.9s 的假长尾), 单独计数不参与统计。
    """
    starts, durations, orders, errs = 0, [], 0, 0
    unpaired, suspicious = 0, 0
    last_start = None
    for ln in lines:
        if "auto_execute 开始" in ln:
            if last_start is not None:
                unpaired += 1
            starts += 1
            m = RE_TS.match(ln)
            last_start = (m.group(1) + "." + m.group(2)) if m else None
        elif "自动执行完成" in ln and last_start:
            m = RE_TS.match(ln)
            if m:
                t0 = datetime.strptime(last_start, "%Y-%m-%d %H:%M:%S.%f")
                t1 = datetime.strptime(m.group(1) + "." + m.group(2), "%Y-%m-%d %H:%M:%S.%f")
                dt = (t1 - t0).total_seconds()
                if dt <= max_pair_s:
                    durations.append(dt)
                else:
                    suspicious += 1
            last_start = None
        elif "下单已受理" in ln:
            orders += 1
        elif "盘中自动执行异常" in ln:
            errs += 1
    if last_start is not None:
        unpaired += 1
    return starts, durations, orders, errs, unpaired, suspicious


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=date.today().isoformat())
    ap.add_argument("--push", action="store_true", help="结果推送到钉钉/飞书")
    args = ap.parse_args()
    day = args.date

    files = _log_files_for(day)
    lines = list(_iter_lines(files, day))
    verdict = []
    out = [f"### FTSeek-US 健康速判 {day} (日志行 {len(lines)}; 文件 {[p.name for p in files]})"]

    # 1. 冻结/重启
    sent, stacks, kills = _freeze_events(day)
    if kills == 0 and not sent:
        out.append("✅ 冻结/重启: 0 次 SIGKILL/SEGV, 哨兵无处置记录")
    else:
        out.append(f"🔴 冻结/重启: SIGKILL/SEGV={kills}, 哨兵处置={len(sent)}, 栈={len(stacks)}")
        for s in sent[:3]:
            out.append("   " + s[:160])
        verdict.append("freeze")

    # 2. GC
    windows, since, gen2, gc_stats, rss = _gc_summary(lines)
    peak = max(rss) if rss else -1
    worst = max((w[1] for w in windows), default=0)
    out.append(
        f"· GC: 最长停顿 本窗口 {worst}ms / 启动以来 {since}ms; gen2 全堆扫描 {gen2} 次; "
        f"RSS 峰值 {peak}MB" + (f"; 代统计 gen0/gen1/gen2 回收 {gc_stats[1]}/{gc_stats[3]}/{gc_stats[5]}" if gc_stats else "")
    )
    if since >= 5000:
        out.append("   ⚠️ 单次停顿 ≥5s: 查该时刻步骤 (RSS 台阶/缺页)"
                   )
        verdict.append("gc")
    if peak >= 1200:
        out.append("   ⚠️ RSS 峰值 ≥1.2GB: 堆已接近宿主能力, 停顿时长会随堆放大")
        verdict.append("rss")

    # 3. 轮次计时
    ph, rss_steps = _phase_stats(lines)
    if ph:
        rows = sorted(ph.items(), key=lambda kv: -max(kv[1]))[:6]
        out.append("· 最慢同步步骤 (次数/最长/累计):")
        for label, ds in rows:
            out.append(f"   {label:34s} n={len(ds):<3d} max={max(ds):6.1f}s sum={sum(ds):7.1f}s"
                       + (f"  RSS台阶x{rss_steps[label]}" if rss_steps[label] else ""))
        top = rows[0]
        if max(top[1]) >= 30:
            out.append(f"   ⚠️ 长尾 {top[0]} {max(top[1]):.0f}s ≥30s — 重活外移候选")
            verdict.append("phase")
    else:
        out.append("· 轮次计时: 无 ≥5s 步骤记录 (休市/空闲, 或尚未跑到)")

    # 4. 执行
    starts, durs, orders, errs, unpaired, suspicious = _exec_stats(lines)
    if starts:
        dmax = max(durs) if durs else -1
        note = ""
        if unpaired or suspicious:
            note = f" (未配对 {unpaired} 轮/跨重启伪配对 {suspicious} 轮, 已排除)"
        out.append(f"· 入场执行: auto_execute {starts} 轮, 单轮最长 {dmax:.1f}s, "
                   f"下单受理 {orders} 笔, 异常 {errs}{note}")
        if errs:
            verdict.append("exec")
    else:
        out.append("· 入场执行: 今日无 auto_execute 轮次")

    out.append("=== 结论: " + ("PASS (无冻结/无长停顿)" if not verdict else "WARN " + ",".join(sorted(set(verdict))) + " ==="))
    text = "\n".join(out)
    print(text)

    if args.push:
        msg = "[FTSeek-US] " + day + " 健康速判\n" + text.replace("### ", "")
        try:
            from app.notifier.dingtalk import DingTalkNotifier  # type: ignore
            DingTalkNotifier().send_text(msg)
            print("\n[push] 已推送 (dingtalk)")
        except Exception as exc:
            print(f"\n[push] 推送失败: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())