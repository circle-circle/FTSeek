#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""v4: 三个决定性切片 — ①死MFE池是否LONG专属 ②费用vs edge(10bps线) ③band宽窄↔成交率相关"""
from pathlib import Path as _P
_ROOT = str(_P(__file__).resolve().parents[1])
import sqlite3, collections, statistics as st
c = sqlite3.connect(f"file:{_ROOT}/data/ftseek_us.db?mode=ro", uri=True); c.row_factory = sqlite3.Row

print("=" * 82)
print("① 死MFE池 (MFE<0.25%) 是否 LONG 专属 — 决定优化靶点是入场还是退出")
print("=" * 82)
rows = c.execute("""SELECT direction d, mfe_pct m, pnl_net n, realized_pnl g, fees_est f, exit_reason e, trade_date
                    FROM trade_records WHERE mfe_pct IS NOT NULL ORDER BY id""").fetchall()
by = collections.defaultdict(list)
for r in rows: by[(str(r["d"] or "?").upper()[:5], "死MFE" if abs(float(r["m"])) < 0.25 else "活MFE")].append(r)
print(f"    {'方向×池':18s} {'n':>5s} {'占比':>7s} {'净额':>11s} {'毛利':>11s} {'费用':>9s} {'中位净':>8s} {'费用/|毛利|':>10s}")
for (d, p), v in sorted(by.items()):
    net = [float(x["n"]) for x in v if x["n"] is not None]
    g = sum(float(x["g"] or 0) for x in v); f = sum(float(x["f"] or 0) for x in v)
    print(f"    {d+' '+p:18s} {len(v):5d} {100*len(v)/len(rows):6.1f}% {sum(net):11.2f} {g:11.2f} {f:9.2f} "
          f"{st.median(net) if net else 0:8.2f} {f/abs(g) if g else 0:9.2f}x")

print()
print("=" * 82)
print("② 费用 vs edge (用户铁律: edge 必须 > ~10bps, 费用≈毛利42倍)")
print("=" * 82)
# 每笔: 费用 bps (相对名义) vs 落袋 bps
rr = c.execute("""SELECT symbol, direction d, entry_price e, exit_price x, quantity q, realized_pnl g,
                         pnl_net n, fees_est f, mfe_pct m, pnl_pct p
                  FROM trade_records WHERE fees_est IS NOT NULL AND fees_est>0 AND entry_price>0 AND quantity>0
                  ORDER BY id""").fetchall()
print(f"  有费用样本 n={len(rr)}")
fee_bps = []; edge_bps = []; mfe_bps = []
for r in rr:
    notional = float(r["e"]) * float(r["q"])
    if notional <= 0: continue
    fee_bps.append(float(r["f"]) / notional * 10000)
    edge_bps.append(float(r["g"] or 0) / notional * 10000)
    mfe_bps.append(abs(float(r["m"] or 0)) * 100)
for nm, v in (("费用 bps", fee_bps), ("毛利 bps", edge_bps), ("MFE bps", mfe_bps)):
    s = sorted(v); k = len(s)
    print(f"  {nm:10s} 中位={st.median(s):7.1f} p25={s[k//4]:7.1f} p75={s[3*k//4]:7.1f} p90={s[int(k*.9)]:7.1f} 均值={st.mean(s):7.1f}")
over = sum(1 for a, b in zip(edge_bps, fee_bps) if a > b)
print(f"  ⇒ 毛利 > 费用 的笔数 = {over}/{len(rr)} ({100*over/len(rr):.1f}%)  【edge>费用线 才是活单】")
over10 = sum(1 for x in edge_bps if x > 10)
print(f"  ⇒ 毛利 > 10bps 的笔数 = {over10}/{len(rr)} ({100*over10/len(rr):.1f}%)")
mfe_over = sum(1 for a, b in zip(mfe_bps, fee_bps) if a > b)
print(f"  ⇒ MFE > 费用 的笔数 = {mfe_over}/{len(rr)} ({100*mfe_over/len(rr):.1f}%)  【曾摸到够付费用的利润】")
print(f"  ⚠ 关键落差: 摸到={100*mfe_over/len(rr):.1f}% 但落袋={100*over/len(rr):.1f}% ⇒ 捕获失败率={100*(mfe_over-over)/max(mfe_over,1):.1f}%")

print()
print("=" * 82)
print("③ band 宽窄 ↔ 成交率 (放宽 band 真能多成交吗?)")
print("=" * 82)
rr = c.execute("""SELECT entry_low lo, entry_high hi, entry_price e, triggered t, executed x, direction d,
                         atr_pct a, vol_bucket vb, date
                  FROM playbook_instructions WHERE entry_low>0 AND entry_high>0 AND entry_price>0
                  ORDER BY id""").fetchall()
print(f"  有 band 样本 n={len(rr)}")
bk = collections.defaultdict(lambda: [0, 0, 0])   # width_bucket -> [n, trig, exec]
for r in rr:
    e = float(r["e"]); w = (float(r["hi"]) - float(r["lo"])) / e * 100
    b = "<4%" if w < 4 else "4-6%" if w < 6 else "6-8%" if w < 8 else "8-10%" if w < 10 else ">=10%"
    bk[b][0] += 1; bk[b][1] += int(bool(r["t"])); bk[b][2] += int(bool(r["x"]))
print(f"    {'band总宽':10s} {'n':>5s} {'触发率':>8s} {'执行率':>8s}")
for b in ("<4%", "4-6%", "6-8%", "8-10%", ">=10%"):
    if b in bk:
        n_, t_, x_ = bk[b]
        print(f"    {b:10s} {n_:5d} {100*t_/n_:7.1f}% {100*x_/n_:7.1f}%")
print(f"  ⇒ 判读: 若触发率随 band 变宽单调上升 = 放宽有量; 若平坦 = band 不是成交瓶颈")

print()
print("=" * 82)
print("④ band 几何 vs entry_band.py 规格 (SSOT 声称是否被数据违反)")
print("=" * 82)
dev = collections.Counter()
for r in rr:
    e = float(r["e"]); lo = float(r["lo"]); hi = float(r["hi"])
    d = str(r["d"] or "?").upper(); short = d.startswith("S")
    kn = (hi - e) / e * 100 if short else (e - lo) / e * 100
    ch = (e - lo) / e * 100 if short else (hi - e) / e * 100
    if abs(kn - 3.0) < 0.02 and abs(ch - 5.0) < 0.02: dev["扁平3/5(规格)"] += 1
    elif 3.0 <= kn <= 6.0 and 5.0 <= ch <= 8.0: dev["vol_widen区间"] += 1
    elif ch < 5.0: dev[f"chase<5%(违反只放宽)"] += 1
    elif kn < 3.0: dev["knife<3%(违反下限)"] += 1
    else: dev["其他/超上限"] += 1
for k, v in dev.most_common():
    print(f"    {k:34s} {v:5d} ({100*v/len(rr):5.1f}%)")
print(f"  ⇒ entry_band.py 声称『vol_widen 只放宽不收窄, chase 上限8%』")
print(f"     若 chase<5% 占比高 = DB 里的 band 不是 compute_entry_band 产出 (SSOT 被绕过)")
