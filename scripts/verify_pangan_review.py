# -*- coding: utf-8 -*-
"""独立验证 Qoder §9.0 两个阻断性发现 (Hermes 复核, 不复用其已删脚本)."""
import json, glob, os, math, random
from collections import defaultdict

ROOT = "/opt/ftseek/FTSeek-US"
bars = {}
for fp in glob.glob(f"{ROOT}/data/replay_bars_daily/US_*.json"):
    sym = "US." + os.path.basename(fp).replace(".json", "").replace("US_", "")
    try:
        rows = json.load(open(fp))
        bars[sym] = [float(r[4]) for r in rows]
    except Exception:
        pass
print(f"载入 {len(bars)} 只")

# ── 验证1a: 循环 bug 实锤 — R1 事件结果是否 100% 为正 ──
tot_r1 = pos_r1 = tot_r2 = neg_r2 = 0
for sym, closes in bars.items():
    n = len(closes)
    if n < 60: continue
    ret1 = [(closes[i+1]-closes[i])/closes[i]*100 for i in range(n-1)] + [0.0]
    up = [r > 0 for r in ret1]
    dn = [r < 0 for r in ret1]
    for i in range(2, n-1):
        if up[i] and up[i-1]:   # 我原脚本的条件 (含结果日 i)
            tot_r1 += 1
            if ret1[i] > 0: pos_r1 += 1
        if dn[i] and dn[i-1]:
            tot_r2 += 1
            if ret1[i] < 0: neg_r2 += 1
print(f"\n[验证1a] 原条件(含结果日): R1 事件 {tot_r1}, 结果为正 {pos_r1} ({pos_r1/tot_r1*100:.1f}%)")
print(f"                   R2 事件 {tot_r2}, 结果为负 {neg_r2} ({neg_r2/tot_r2*100:.1f}%)")
print("→ 若 100%: 循环 bug 实锤 (条件包含结果, 符号代数必然)")

# ── 验证1b: 修正版 (条件=两根已完结K线) 重跑 ──
def tstat(vals):
    n = len(vals)
    if n < 2: return 0.0, 0.0
    m = sum(vals)/n
    var = sum((v-m)**2 for v in vals)/(n-1)
    sd = math.sqrt(var) if var > 0 else 1e-9
    return m, m/(sd/math.sqrt(n))

def gate_pass(vals):
    if len(vals) < 30: return False, 0, 0
    m, t = tstat(vals)
    h = len(vals)//2
    im, it = tstat(vals[:h]); om, ot = tstat(vals[h:])
    same = (im < 0 and om < 0) or (im > 0 and om > 0)
    return abs(t) > 1.5 and same, m, t

fix_r1_pass = fix_r2_pass = 0
fix_r1_exc, fix_r2_exc = [], []
for sym, closes in bars.items():
    n = len(closes)
    if n < 60: continue
    ret1 = [(closes[i+1]-closes[i])/closes[i]*100 for i in range(n-1)] + [0.0]
    base = sum(ret1[:-1])/len(ret1[:-1])
    up = [r > 0 for r in ret1]; dn = [r < 0 for r in ret1]
    e1, e2 = [], []
    for i in range(3, n-1):
        if up[i-1] and up[i-2]:   # 修正: 两根已完结K线
            e1.append(ret1[i])
        if dn[i-1] and dn[i-2]:
            e2.append(ret1[i])
    ok1, m1, _ = gate_pass(e1)
    ok2, m2, _ = gate_pass(e2)
    if ok1: fix_r1_pass += 1
    if ok2: fix_r2_pass += 1
    if e1: fix_r1_exc.append(sum(e1)/len(e1) - base)
    if e2: fix_r2_exc.append(sum(e2)/len(e2) - base)

print(f"\n[验证1b] 修正版 PASS: R1fix {fix_r1_pass}/110, R2fix {fix_r2_pass}/110 (Qoder称 17/19)")
print(f"  基线校正群体均值: R1fix {sum(fix_r1_exc)/len(fix_r1_exc):+.3f}pp, "
      f"R2fix {sum(fix_r2_exc)/len(fix_r2_exc):+.3f}pp (Qoder称 ≈0)")
sgn = sum(1 for x in fix_r1_exc if x > 0)
print(f"  R1fix 符号分布: {sgn}正/{len(fix_r1_exc)-sgn}负 (Qoder称 77/33)")

# ── 验证2: 安慰剂校准 — 随机抽同n事件日过三门 ──
rnd = random.Random(20260819)
def placebo(sizes, iters=50, nsym=30):
    rates = []
    syms = [s for s in list(bars) if len(bars[s]) >= 600]
    for _ in range(iters):
        passed = 0; tested = 0
        for sym in rnd.sample(syms, min(nsym, len(syms))):
            closes = bars[sym]; n = len(closes)
            ret1 = [(closes[i+1]-closes[i])/closes[i]*100 for i in range(n-1)]
            for sz in sizes:
                idx = rnd.sample(range(n-1), min(sz, n-1))
                vals = [ret1[i] for i in idx]
                ok, _, _ = gate_pass(vals)
                tested += 1
                if ok: passed += 1
        rates.append(passed/tested*100)
    return sum(rates)/len(rates), min(rates), max(rates)

m600, lo600, hi600 = placebo([600])
m80, lo80, hi80 = placebo([80])
print(f"\n[验证2] 安慰剂零假设通过率 (30票×50次抽样):")
print(f"  n=600: {m600:.1f}% (范围 {lo600:.0f}-{hi600:.0f}%) | Qoder称 24.3%")
print(f"  n=80:  {m80:.1f}% (范围 {lo80:.0f}-{hi80:.0f}%) | Qoder称 14.3%")
print(f"  → 110票期望假PASS: n600类≈{m600*1.1:.0f}只, n80类≈{m80*1.1:.0f}只")
print(f"     观察值: R3=33 R4=19 | R5=18 R6=6 R7=8 R8=9")
