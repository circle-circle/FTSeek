#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""⑯ C3 口径修正复算 (自曝: 前一版用 actual_executed 是错的).
   官方 C3 = 反事实限价腿 status ∈ {entered, expired_unfilled}, 分母=entered+expired, 排除 skip_no_level.
   actual_executed = 真实市价腿(生产实际发生), 两者是不同臂, 不可混用."""
from pathlib import Path as _P
_ROOT = str(_P(__file__).resolve().parents[1])
import json, collections, statistics as st
P = f"{_ROOT}/data/shadow/pullback_limit_replay_v2.jsonl"
rows = [json.loads(l) for l in open(P, encoding="utf-8") if l.strip()]
print(f"jsonl 全行 n={len(rows)}")
st_ = collections.Counter(str(r.get("status")) for r in rows)
print(f"status 分布: {dict(st_)}")
pop = [r for r in rows if str(r.get("status")) in ("entered", "expired_unfilled")]
print(f"官方 C3 分母 (entered+expired) = {len(pop)}   [读数卡声称 104+59=163]")

print()
print("=" * 88)
print("C3 可达性梯度 (修正口径: status==entered 为限价腿成交)")
print("=" * 88)
bk = collections.defaultdict(lambda: [0, 0])
for r in pop:
    d = r.get("reach_dist_atr")
    if d is None: continue
    b = "可达(≤0.5ATR)" if float(d) <= 0.5 else "不可达(>0.5ATR)"
    bk[b][0] += 1; bk[b][1] += 1 if str(r.get("status")) == "entered" else 0
for k, v in sorted(bk.items()):
    print(f"  {k:16s} n={v[0]:4d} entered={v[1]:3d} fill={v[1]/v[0] if v[0] else 0:.3f}")
print(f"  ⇒ 读数卡声称: 可达 n=104 fill=0.51 | 不可达 n=59 fill=0.017")

print()
print("  --- 细粒度 (读数卡的子桶: <0.25 / 0.25-0.5) ---")
bk2 = collections.defaultdict(lambda: [0, 0])
for r in pop:
    d = r.get("reach_dist_atr")
    if d is None: continue
    d = float(d)
    b = "<0.25ATR" if d < 0.25 else "0.25-0.5" if d <= 0.5 else "0.5-1.0" if d < 1.0 else ">1.0"
    bk2[b][0] += 1; bk2[b][1] += 1 if str(r.get("status")) == "entered" else 0
prev = None
for b in ("<0.25ATR", "0.25-0.5", "0.5-1.0", ">1.0"):
    if b not in bk2: continue
    n_, e_ = bk2[b]; rate = e_ / n_ if n_ else 0
    flag = " ← 非单调" if prev is not None and rate > prev + 0.02 else ""
    print(f"    {b:12s} n={n_:4d} entered={e_:3d} fill={rate*100:6.1f}%{flag}")
    prev = rate
print(f"  ⇒ 读数卡声称子桶: <0.25ATR n=55 fill=0.745 | 0.25-0.5ATR n=49 fill=0.245")

print()
print("=" * 88)
print("两臂对照 (这才是 RFC-118 要裁的东西)")
print("=" * 88)
print(f"  {'臂':22s} {'n':>5s} {'成交/entered':>13s} {'率':>8s}")
for nm, pred in (("限价腿(反事实) status=entered", lambda r: str(r.get("status")) == "entered"),
                 ("市价腿(真实生产) actual_executed", lambda r: bool(r.get("actual_executed")))):
    v = [r for r in rows if pred(r)]
    print(f"  {nm:22s} {len(rows):5d} {len(v):13d} {100*len(v)/len(rows):7.1f}%")
print()
print("  --- 分可达性的两臂对照 (核心: 门内限价真能成交吗) ---")
for lab, sel in (("可达(≤0.5ATR)", lambda d: d <= 0.5), ("不可达(>0.5ATR)", lambda d: d > 0.5)):
    g = [r for r in pop if r.get("reach_dist_atr") is not None and sel(float(r["reach_dist_atr"]))]
    lim = sum(1 for r in g if str(r.get("status")) == "entered")
    mkt = sum(1 for r in g if r.get("actual_executed"))
    print(f"    {lab:16s} n={len(g):4d}  限价腿 fill={lim/max(len(g),1)*100:5.1f}%  市价腿 fill={mkt/max(len(g),1)*100:5.1f}%")

print()
print("=" * 88)
print("误杀面 (G5): expired_unfilled 里真实市价腿赚钱的比例")
print("=" * 88)
exp = [r for r in pop if str(r.get("status")) == "expired_unfilled"]
known = [r for r in exp if r.get("actual_net") is not None]
win = [r for r in known if float(r.get("actual_net") or 0) > 0]
print(f"  expired n={len(exp)}  actual_net 已知 n={len(known)}  其中赢家 n={len(win)}")
print(f"  ⇒ 全窗误杀率 = {len(win)/max(len(known),1)*100:.1f}%  (G5 门 ≤40%)")
nets = [float(r["actual_net"] or 0) for r in known]
if nets:
    print(f"  误杀批的真实净额: 总=${sum(nets):.2f} 中位=${st.median(nets):.2f}")
    pos = sorted([x for x in nets if x > 0], reverse=True)
    print(f"  其中赢家总=${sum(pos):.2f} ({100*sum(pos[:3])/sum(pos) if pos else 0:.0f}% 来自 Top-3)")
print()
print("  --- 按可达性拆误杀 (门外回退市价 ⇒ 误杀只算门内) ---")
for lab, sel in (("门内可达", lambda d: d <= 0.5), ("门外不可达", lambda d: d > 0.5)):
    g = [r for r in exp if r.get("reach_dist_atr") is not None and sel(float(r["reach_dist_atr"])) and r.get("actual_net") is not None]
    w = [r for r in g if float(r["actual_net"] or 0) > 0]
    nn = [float(r["actual_net"] or 0) for r in g]
    print(f"    {lab:12s} expired已知n={len(g):3d} 赢家={len(w):3d} 误杀率={len(w)/max(len(g),1)*100:5.1f}% 净额=${sum(nn):9.2f}")
