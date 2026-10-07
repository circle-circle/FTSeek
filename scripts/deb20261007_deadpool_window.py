#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""⑪ 死MFE池时间窗拆分: 是历史包袱(旧架构)还是现役问题?
   分界: 2026-06-27 (C12 人群口径的 playbook 时代起点) / 参数冻结日"""
from pathlib import Path as _P
_ROOT = str(_P(__file__).resolve().parents[1])
import sqlite3, collections, statistics as st
c = sqlite3.connect(f"file:{_ROOT}/data/ftseek_us.db?mode=ro", uri=True); c.row_factory = sqlite3.Row

rows = c.execute("""SELECT trade_date d, symbol s, direction dr, exit_reason e, pnl_net n, realized_pnl g,
                           fees_est f, mfe_pct m, mae_pct a, entry_price ep, quantity q, entry_session es, mode md
                    FROM trade_records WHERE mfe_pct IS NOT NULL ORDER BY trade_date""").fetchall()
print(f"全量 n={len(rows)}  日期 {rows[0]['d']} ~ {rows[-1]['d']}")

WINDOWS = [("~2026-06-26 (旧架构)", lambda d: d < "2026-06-27"),
           ("2026-06-27~08-31", lambda d: "2026-06-27" <= d <= "2026-08-31"),
           ("2026-09-01~09-30", lambda d: "2026-09-01" <= d <= "2026-09-30"),
           ("2026-10-01~ (现役)", lambda d: d >= "2026-10-01")]

print()
print("=" * 100)
print("⑪ 死MFE池 (MFE<0.25%) 时间窗拆分 — 判历史包袱 vs 现役问题")
print("=" * 100)
print(f"  {'窗口':26s} {'n':>5s} {'死池n':>6s} {'死池占比':>8s} {'死池净额':>11s} {'活池净额':>11s} {'窗净':>10s} {'死池WR':>7s} {'活池WR':>7s}")
for nm, fn in WINDOWS:
    v = [r for r in rows if fn(str(r["d"])[:10])]
    if not v: continue
    dead = [r for r in v if abs(float(r["m"] or 0)) < 0.25]
    live = [r for r in v if abs(float(r["m"] or 0)) >= 0.25]
    dn = [float(x["n"]) for x in dead if x["n"] is not None]
    ln = [float(x["n"]) for x in live if x["n"] is not None]
    alln = [float(x["n"]) for x in v if x["n"] is not None]
    dwr = 100 * sum(1 for x in dn if x > 0) / len(dn) if dn else 0
    lwr = 100 * sum(1 for x in ln if x > 0) / len(ln) if ln else 0
    print(f"  {nm:26s} {len(v):5d} {len(dead):6d} {100*len(dead)/len(v):7.1f}% {sum(dn):11.2f} {sum(ln):11.2f} "
          f"{sum(alln):10.2f} {dwr:6.1f}% {lwr:6.1f}%")

print()
print("=" * 100)
print("⑫ 现役窗 (9/1 起) 细看: 死MFE池还有多少可治理空间")
print("=" * 100)
cur = [r for r in rows if str(r["d"])[:10] >= "2026-09-01"]
dead = [r for r in cur if abs(float(r["m"] or 0)) < 0.25]
live = [r for r in cur if abs(float(r["m"] or 0)) >= 0.25]
print(f"  9/1 起 n={len(cur)}  死池 n={len(dead)} ({100*len(dead)/len(cur):.1f}%)  活池 n={len(live)}")
dn = [float(x["n"]) for x in dead if x["n"] is not None]
ln = [float(x["n"]) for x in live if x["n"] is not None]
print(f"  死池 净=${sum(dn):.2f} 中位单笔=${st.median(dn) if dn else 0:.2f} WR={100*sum(1 for x in dn if x>0)/max(len(dn),1):.1f}%")
print(f"  活池 净=${sum(ln):.2f} 中位单笔=${st.median(ln) if ln else 0:.2f} WR={100*sum(1 for x in ln if x>0)/max(len(ln),1):.1f}%")
print(f"  窗净=${sum(dn)+sum(ln):.2f}")
# 死池费用占比
df = sum(float(x["f"] or 0) for x in dead); dg = sum(float(x["g"] or 0) for x in dead)
lf = sum(float(x["f"] or 0) for x in live); lg = sum(float(x["g"] or 0) for x in live)
print(f"\n  死池: 毛利=${dg:.2f} 费用=${df:.2f} 费用/|毛利|={df/abs(dg) if dg else 0:.2f}x")
print(f"  活池: 毛利=${lg:.2f} 费用=${lf:.2f} 费用/|毛利|={lf/abs(lg) if lg else 0:.2f}x")
print(f"  ⇒ 死池的亏损构成: 毛利亏 ${dg:.2f} + 费用 ${df:.2f}")
print(f"     若死池能被入场门全拦: 挽回 ${abs(sum(dn)):.2f} (但误杀风险需按活池WR评估)")

print()
print("  --- 9/1 起死池的出场原因 (判是入场即错还是退出太晚) ---")
by = collections.defaultdict(list)
for x in dead: by[str(x["e"] or "NULL")].append(x)
for k, v in sorted(by.items(), key=lambda kv: sum(float(x["n"] or 0) for x in kv[1]))[:12]:
    vn = [float(x["n"]) for x in v if x["n"] is not None]
    ma = [abs(float(x["a"] or 0)) for x in v if x["a"] is not None]
    print(f"    {k:26s} n={len(v):3d} 净=${sum(vn):9.2f} 中位=${st.median(vn) if vn else 0:8.2f} "
          f"MAE中位={st.median(ma) if ma else 0:5.2f}%")

print()
print("  --- 9/1 起死池按方向 (LONG/SHORT 是否不对称) ---")
by = collections.defaultdict(list)
for x in dead: by[str(x["dr"] or "?").upper()[:5]].append(x)
for k, v in sorted(by.items(), key=lambda kv: -len(kv[1])):
    vn = [float(x["n"]) for x in v if x["n"] is not None]
    print(f"    {k:8s} n={len(v):3d} 净=${sum(vn):9.2f} 中位单笔=${st.median(vn) if vn else 0:7.2f} WR={100*sum(1 for x in vn if x>0)/max(len(vn),1):5.1f}%")
print()
print("  --- 9/1 起活池按方向 (对照) ---")
by = collections.defaultdict(list)
for x in live: by[str(x["dr"] or "?").upper()[:5]].append(x)
for k, v in sorted(by.items(), key=lambda kv: -len(kv[1])):
    vn = [float(x["n"]) for x in v if x["n"] is not None]
    print(f"    {k:8s} n={len(v):3d} 净=${sum(vn):9.2f} 中位单笔=${st.median(vn) if vn else 0:7.2f} WR={100*sum(1 for x in vn if x>0)/max(len(vn),1):5.1f}%")

print()
print("=" * 100)
print("⑬ 费用线实证 (用户铁律 edge>~10bps)")
print("=" * 100)
cur2 = [r for r in cur if r["f"] and float(r["f"]) > 0 and r["ep"] and r["q"]]
fb = sorted(float(r["f"]) / (float(r["ep"]) * float(r["q"])) * 10000 for r in cur2)
gb = sorted(float(r["g"] or 0) / (float(r["ep"]) * float(r["q"])) * 10000 for r in cur2)
k = len(fb)
print(f"  9/1 起 n={k}")
print(f"  费用 bps: 中位={st.median(fb):.1f} p25={fb[k//4]:.1f} p75={fb[3*k//4]:.1f} p90={fb[int(k*.9)]:.1f}")
print(f"  毛利 bps: 中位={st.median(gb):.1f} p25={gb[k//4]:.1f} p75={gb[3*k//4]:.1f} p90={gb[int(k*.9)]:.1f}")
print(f"  毛利>费用 笔数 = {sum(1 for a,b in zip(gb,fb) if a>b)}/{k}")
# 名义规模分桶: 小额单费用拖累
print(f"\n  --- 按名义规模分桶 (费用 bps 与规模的关系) ---")
bk = collections.defaultdict(list)
for r in cur2:
    no = float(r["ep"]) * float(r["q"])
    b = "<$3k" if no < 3000 else "$3-6k" if no < 6000 else "$6-10k" if no < 10000 else "$10-20k" if no < 20000 else ">$20k"
    bk[b].append((float(r["f"]) / no * 10000, float(r["g"] or 0) / no * 10000, r))
for b in ("<$3k", "$3-6k", "$6-10k", "$10-20k", ">$20k"):
    if b not in bk: continue
    v = bk[b]
    f_ = st.median([x[0] for x in v]); g_ = st.median([x[1] for x in v])
    net = sum(float(x[2]["n"] or 0) for x in v)
    print(f"    {b:9s} n={len(v):4d} 费用中位={f_:6.1f}bp 毛利中位={g_:7.1f}bp 净=${net:9.2f} "
          f"{'⚠费用>毛利' if f_ > g_ else ''}")
