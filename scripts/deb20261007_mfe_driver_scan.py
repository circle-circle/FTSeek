#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""[DEB-20261007 追问 · 阶段3] 既然入场价位移轴零判别力, MFE 由什么决定?

目的: 给用户可动的杠杆, 不只是否证。
纪律:
  - 只扫**入场时刻可知**的变量 (T19: mfe/mae/exit_reason 是事后量, 禁作门输入)
  - 分档单调性 + 点级 Spearman 并报 (T41)
  - 每档带 n, n<30 只观察不结论 (MEMORY: 每组≥30)
  - 日聚类 t (T39)
  - 三窗同号 (全/前半/后半) 才可引用 (T43 统计族门槛 |IC|≥0.08)
"""
from pathlib import Path as _P
_ROOT = str(_P(__file__).resolve().parents[1])
import sqlite3, json, math, statistics as st, collections
from datetime import datetime

c = sqlite3.connect(f"file:{_ROOT}/data/ftseek_us.db?mode=ro", uri=True)
c.row_factory = sqlite3.Row

rows = c.execute("""SELECT id, date, symbol, direction d, entry_price e, execution_time xt, created_at ca,
                           atr_pct atr, vol_bucket vb, confidence conf, market_regime mr,
                           vix_at_generation vix, spy_trend_at_generation spy, horizon,
                           raw_instruction_json raw, decision_id did, sector
                    FROM playbook_instructions ORDER BY id""").fetchall()

tr = {}
for t in c.execute("SELECT * FROM trade_records WHERE source_type='PLAYBOOK_SIM'"):
    k = str(t["decision_id"] or "")
    if k: tr.setdefault(k, []).append(t)

S = []
for r in rows:
    did = str(r["did"] or "")
    if did not in tr: continue
    t = tr[did][0]
    if t["mfe_pct"] is None: continue
    try: raw = json.loads(r["raw"]) if r["raw"] else {}
    except Exception: raw = {}
    if not isinstance(raw, dict): raw = {}
    hh = None
    for f in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            hh = datetime.strptime(str(r["xt"]).replace("Z", ""), f).hour; break
        except Exception: continue
    S.append(dict(id=r["id"], td=t["trade_date"], sym=r["symbol"],
                  mfe=float(t["mfe_pct"]),
                  pnl=float(t["pnl_net"]) if t["pnl_net"] is not None else None,
                  er=t["exit_reason"],
                  short=str(r["d"] or "").upper().startswith("S"),
                  hour=hh, atr=float(r["atr"]) if r["atr"] else None,
                  conf=float(r["conf"]) if r["conf"] else None,
                  vb=r["vb"], mr=r["mr"], vix=float(r["vix"]) if r["vix"] else None,
                  arch=raw.get("entry_archetype"), sector=r["sector"],
                  notional=float(r["e"]) * float(t["quantity"]) if r["e"] and t["quantity"] else None))

print(f"人群 n={len(S)}  (有成交 ∧ 有 mfe_pct)")


def spear(xs, ys):
    n = len(xs)
    if n < 5: return None
    rx = {v: i for i, v in enumerate(sorted(xs))}; ry = {v: i for i, v in enumerate(sorted(ys))}
    dx = [rx[v]-(n-1)/2 for v in xs]; dy = [ry[v]-(n-1)/2 for v in ys]
    den = math.sqrt(sum(a*a for a in dx)*sum(b*b for b in dy))
    return sum(a*b for a, b in zip(dx, dy))/den if den else None


def dayt(pairs):
    byd = collections.defaultdict(list)
    for d, v in pairs:
        if v is not None: byd[d].append(v)
    if len(byd) < 3: return None, len(byd)
    ds = [st.mean(v) for v in byd.values()]
    sd = st.stdev(ds) if len(ds) > 1 else 0
    return (st.mean(ds)/(sd/math.sqrt(len(ds))) if sd > 1e-12 else None), len(byd)


print("\n" + "=" * 104)
print("① 事后量基准 (确认 MFE 是不是净利的决定量 — 这决定「提 MFE」值不值得)")
print("=" * 104)
both = [(s["mfe"], s["pnl"]) for s in S if s["pnl"] is not None]
print(f"  Spearman(mfe_pct, pnl_net) = {round(spear([x[0] for x in both],[x[1] for x in both]),4)}  n={len(both)}")
tm, nd = dayt([(s["td"], s["mfe"]) for s in S])
print(f"  mfe_pct 全体: 中位={st.median([s['mfe'] for s in S]):.4f}% 均值={st.mean([s['mfe'] for s in S]):+.4f}% 日聚类t={round(tm,2) if tm else 'NA'} (日数{nd})")
# 按 exit_reason 看 mfe 与 pnl 的关系
print("\n  按 exit_reason (系统净利来自哪个出口):")
by_er = collections.defaultdict(list)
for s in S: by_er[str(s["er"])].append(s)
for k, v in sorted(by_er.items(), key=lambda kv: -sum(x["pnl"] or 0 for x in kv[1])):
    pn = [x["pnl"] for x in v if x["pnl"] is not None]
    mf = [x["mfe"] for x in v]
    print(f"    {k:22s} n={len(v):3d} Σpnl=${sum(pn):+9.2f} 笔均=${st.mean(pn) if pn else 0:+7.2f} "
          f"WR={100*sum(1 for x in pn if x>0)/len(pn) if pn else 0:3.0f}% mfe中位={st.median(mf):.4f}%")

print("\n" + "=" * 104)
print("② 入场时刻可知变量 × MFE 扫描 (三窗 IC 同号 + |IC|≥0.08 才可引用)")
print("=" * 104)
Ss = sorted(S, key=lambda s: (s["td"], s["id"])); h = len(Ss)//2
wins = [("全窗", Ss), ("前半", Ss[:h]), ("后半", Ss[h:])]
print(f"  窗口: 前半 {Ss[0]['td']}..{Ss[h-1]['td']}  后半 {Ss[h]['td']}..{Ss[-1]['td']} (不重叠)")
cands = []
for nm, get in (("confidence", lambda s: s["conf"]), ("atr_pct", lambda s: s["atr"]),
                ("vix_at_gen", lambda s: s["vix"]), ("入场小时(ET近似)", lambda s: s["hour"]),
                ("notional", lambda s: s["notional"])):
    ics = []
    for wn, g in wins:
        p = [(get(s), s["mfe"]) for s in g if get(s) is not None]
        ics.append((wn, spear([x[0] for x in p], [x[1] for x in p]), len(p)))
    ok = all(i[1] is not None for i in ics) and (ics[0][1]*ics[1][1] > 0) and (ics[0][1]*ics[2][1] > 0) \
         and abs(ics[0][1] or 0) >= 0.08
    cands.append((nm, ics, ok))
    print(f"  {nm:18s} " + "  ".join(f"{w}: IC={round(i,4) if i is not None else 'NA'}(n={n})" for w, i, n in ics)
          + f"   ⇒ {'✅三窗同号且|IC|≥0.08' if ok else '❌不可引用'}")

for nm, keyf in (("entry_archetype", lambda s: s["arch"]), ("vol_bucket", lambda s: s["vb"]),
                 ("market_regime", lambda s: s["mr"]), ("方向", lambda s: "SHORT" if s["short"] else "LONG")):
    print(f"\n  --- {nm} 分档 (n<30 只观察) ---")
    by = collections.defaultdict(list)
    for s in S: by[str(keyf(s))].append(s)
    for k, v in sorted(by.items(), key=lambda kv: -len(kv[1])):
        mf = sorted(x["mfe"] for x in v); pn = [x["pnl"] for x in v if x["pnl"] is not None]
        t, ndy = dayt([(x["td"], x["mfe"]) for x in v])
        flag = "" if len(v) >= 30 else "  ⚠️n<30只观察"
        print(f"    {k:26s} n={len(v):3d} mfe中位={st.median(mf):.4f}% 均值={st.mean(mf):+.4f}% "
              f"日聚类t={round(t,2) if t else 'NA'} 有肉率(≥1.5%)={100*sum(1 for x in mf if x>=1.5)/len(mf):.0f}% "
              f"笔均pnl=${st.mean(pn) if pn else 0:+.2f}{flag}")

print("\n" + "=" * 104)
print("③ 时段轴 (入场小时) 分档 — 唯一可能可动的入场侧杠杆")
print("=" * 104)
by = collections.defaultdict(list)
for s in S:
    if s["hour"] is not None: by[s["hour"]].append(s)
for k in sorted(by):
    v = by[k]; mf = sorted(x["mfe"] for x in v); pn = [x["pnl"] for x in v if x["pnl"] is not None]
    flag = "" if len(v) >= 30 else "  ⚠️n<30只观察"
    print(f"  {k:02d}时 n={len(v):3d} mfe中位={st.median(mf):.4f}% 有肉率={100*sum(1 for x in mf if x>=1.5)/len(mf):.0f}% "
          f"死单率(<0.3%)={100*sum(1 for x in mf if x<0.3)/len(mf):.0f}% 笔均pnl=${st.mean(pn) if pn else 0:+.2f}{flag}")

print("\n" + "=" * 104)
print("④ 反手禁令自查: Q5(追价最多)反而 mfe 最高 — 是动量共线还是入场价因果?")
print("=" * 104)
print("  共线机制: adverse 大 ⇔ 指令生成后价格上涨多 ⇔ 当日动量强 ⇒ mfe 高来自动量, 不来自「买贵」本身")
print("  判别所需对照 = 当日开盘→入场时刻涨幅 (DB 无此字段, 需 1m bar) ⇒ 本轮不下因果结论")
print("  纪律: 不得反手使用「追价更好」(T28 post-hoc inversion ban) — 它是在同一批数据上回头挑出的形状")
