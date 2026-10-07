#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""[DEB-20261007 追问 · 阶段4] 唯一过门的入场侧变量 (atr_pct) 是否真能提净利? + 时段轴重跑.

修正: 阶段3 用 playbook_instructions.execution_time 取入场小时, 非空率仅 645/3377 ⇒ n=11 是
      **取数字段错配** (T44-④), 不是数据不存在。正确源 = trade_records.entry_time (612/612 = 100%,
      带 ET 时区 -04:00)。本脚本重跑时段轴。

关键追问 (决定 atr_pct 能不能用):
  ① atr_pct → mfe IC 三窗同号 (0.128/0.132/0.078, 后半略低于 0.08 门槛 ⇒ 边界, 须如实声明)
  ② 但 **MFE 高 ≠ 净利高**: ATR 大 ⇒ SL 距离大 ⇒ 单笔亏损也大。必须查 atr_pct → pnl_net
     是否同向, 且查它是不是只是"仓位变小"的算术结果 (风险平价下 notional ∝ 1/ATR)
  ③ 分档单调性 (T13-2): 只有 IC 没有单调性 ⇒ 不可设门
  ④ 与 exit_reason 结构的交互: 高 ATR 组是否更多走 SL_INITIAL (那个 −$4474 的出口)
"""
from pathlib import Path as _P
_ROOT = str(_P(__file__).resolve().parents[1])
import sqlite3, json, math, statistics as st, collections
from datetime import datetime

c = sqlite3.connect(f"file:{_ROOT}/data/ftseek_us.db?mode=ro", uri=True)
c.row_factory = sqlite3.Row

inst = {}
for r in c.execute("""SELECT decision_id, atr_pct, confidence conf, vol_bucket vb, entry_price e,
                             position_size ps, risk_amount ra, direction d, date
                      FROM playbook_instructions WHERE decision_id IS NOT NULL"""):
    k = str(r["decision_id"])
    if k not in inst: inst[k] = r

S = []
for t in c.execute("SELECT * FROM trade_records WHERE source_type='PLAYBOOK_SIM'"):
    if t["mfe_pct"] is None: continue
    k = str(t["decision_id"] or "")
    r = inst.get(k)
    et = None
    try:
        et = datetime.fromisoformat(str(t["entry_time"])) if t["entry_time"] else None
    except Exception:
        for f in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S.%f"):
            try: et = datetime.strptime(str(t["entry_time"]), f); break
            except Exception: continue
    S.append(dict(td=t["trade_date"], sym=t["symbol"], mfe=float(t["mfe_pct"]),
                  mae=float(t["mae_pct"]) if t["mae_pct"] is not None else None,
                  pnl=float(t["pnl_net"]) if t["pnl_net"] is not None else None,
                  rpnl=float(t["realized_pnl"]) if t["realized_pnl"] is not None else None,
                  er=str(t["exit_reason"] or ""), sess=t["entry_session"],
                  hour=(et.hour if et else None), minute=(et.minute if et else None),
                  et=et, atr=float(r["atr_pct"]) if (r and r["atr_pct"]) else None,
                  conf=float(r["conf"]) if (r and r["conf"]) else None,
                  qty=float(t["quantity"]) if t["quantity"] else None,
                  ep=float(t["entry_price"]) if t["entry_price"] else None,
                  ra=float(r["ra"]) if (r and r["ra"]) else None,
                  short=str(r["d"] or "").upper().startswith("S") if r else None))

n_et = sum(1 for s in S if s["hour"] is not None)
print(f"人群 n={len(S)}  入场时刻可解析 n={n_et} ({100*n_et/len(S):.0f}%)  ← 阶段3 的 n=11 是字段错配")


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
print("① 时段轴重跑 (正确字段 trade_records.entry_time, ET)")
print("=" * 104)
by = collections.defaultdict(list)
for s in S:
    if s["hour"] is not None: by[s["hour"]].append(s)
for k in sorted(by):
    v = by[k]; mf = sorted(x["mfe"] for x in v); pn = [x["pnl"] for x in v if x["pnl"] is not None]
    t, nd = dayt([(x["td"], x["mfe"]) for x in v])
    flag = "" if len(v) >= 30 else "  ⚠️n<30只观察"
    print(f"  {k:02d}时 n={len(v):3d} 日数={nd:2d} mfe中位={st.median(mf):.4f}% 有肉率={100*sum(1 for x in mf if x>=1.5)/len(mf):3.0f}% "
          f"死单率={100*sum(1 for x in mf if x<0.3)/len(mf):3.0f}% 笔均pnl=${st.mean(pn) if pn else 0:+8.2f} "
          f"Σpnl=${sum(pn) if pn else 0:+9.2f} mfe日聚类t={round(t,2) if t else 'NA'}{flag}")
# 时段三分 (开盘30min / 中段 / 尾盘) 保证 n≥30
seg = collections.defaultdict(list)
for s in S:
    if s["hour"] is None: continue
    m = s["hour"]*60 + (s["minute"] or 0)
    k = "09:30-10:00(开盘)" if m < 600 else ("10:00-15:00(中段)" if m < 900 else "15:00-16:00(尾盘)")
    if s["hour"] < 9: k = "盘前/延长时段"
    seg[k].append(s)
print("\n  --- 时段三分 (n≥30 保证) ---")
for k in sorted(seg):
    v = seg[k]; mf = sorted(x["mfe"] for x in v); pn = [x["pnl"] for x in v if x["pnl"] is not None]
    t, nd = dayt([(x["td"], x["mfe"]) for x in v])
    tp, _ = dayt([(x["td"], x["pnl"]) for x in v])
    flag = "" if len(v) >= 30 else "  ⚠️n<30只观察"
    print(f"  {k:22s} n={len(v):3d} 日数={nd:2d} mfe中位={st.median(mf):.4f}% 有肉率={100*sum(1 for x in mf if x>=1.5)/len(mf):3.0f}% "
          f"死单率={100*sum(1 for x in mf if x<0.3)/len(mf):3.0f}% | 笔均pnl=${st.mean(pn) if pn else 0:+8.2f} "
          f"Σ=${sum(pn) if pn else 0:+9.2f} pnl日聚类t={round(tp,2) if tp else 'NA'}{flag}")
print("\n  --- entry_session 值域 (T44-④: 分组前先印值域) ---")
print("  ", dict(collections.Counter(str(s["sess"]) for s in S)))

print("\n" + "=" * 104)
print("② atr_pct → 净利 (不只 MFE): 高波动是收益来源还是亏损来源?")
print("=" * 104)
A = [s for s in S if s["atr"] is not None]
print(f"  n={len(A)}")
for nm, tgt in (("mfe_pct", "mfe"), ("mae_pct", "mae"), ("pnl_net", "pnl")):
    p = [(s["atr"], s[tgt]) for s in A if s[tgt] is not None]
    sp = spear([x[0] for x in p], [x[1] for x in p])
    print(f"  Spearman(atr_pct, {nm:8s}) = {round(sp,4) if sp is not None else 'NA':>8}  n={len(p)}")
Ss = sorted(A, key=lambda s: (s["td"], s["sym"])); h = len(Ss)//2
print("\n  三窗 IC (atr_pct → 目标):")
for nm, tgt in (("mfe", "mfe"), ("mae", "mae"), ("pnl_net", "pnl")):
    ics = []
    for wn, g in (("全窗", A), ("前半", Ss[:h]), ("后半", Ss[h:])):
        p = [(s["atr"], s[tgt]) for s in g if s[tgt] is not None]
        ics.append((wn, spear([x[0] for x in p], [x[1] for x in p]), len(p)))
    same = all(i[1] is not None for i in ics) and ics[0][1]*ics[1][1] > 0 and ics[0][1]*ics[2][1] > 0
    print(f"    {nm:8s} " + "  ".join(f"{w}: {round(i,4) if i is not None else 'NA'}(n={n})" for w, i, n in ics)
          + f"  ⇒ {'三窗同号' if same else '❌翻号'} {'且|IC|≥0.08' if abs(ics[0][1] or 0)>=0.08 else '但|IC|<0.08'}")

print("\n  --- atr_pct 四分档 (单调性 T13-2 + 净利与出口结构) ---")
qs = sorted(s["atr"] for s in A)
cuts = [qs[int(len(qs)*p)] for p in (0.25, 0.5, 0.75)]
labs = ["A1(最低波)", "A2", "A3", "A4(最高波)"]
bk = collections.defaultdict(list)
for s in A:
    i = 0
    while i < 3 and s["atr"] > cuts[i]: i += 1
    bk[i].append(s)
for i in range(4):
    v = bk[i]
    if not v: continue
    mf = sorted(x["mfe"] for x in v); pn = [x["pnl"] for x in v if x["pnl"] is not None]
    ma = [x["mae"] for x in v if x["mae"] is not None]
    tp, nd = dayt([(x["td"], x["pnl"]) for x in v])
    ers = collections.Counter(x["er"] for x in v)
    worst = sum(cnt for er, cnt in ers.items() if er in ("SL_INITIAL", "SL_ATR_STOP", "OVERNIGHT_GAP_STOP"))
    print(f"  {labs[i]:11s} atr∈[{cuts[i-1] if i else min(qs):.4f},{cuts[i] if i<3 else max(qs):.4f}] n={len(v):3d} "
          f"mfe中位={st.median(mf):.4f}% mae中位={st.median(ma) if ma else 0:.4f}% "
          f"笔均pnl=${st.mean(pn) if pn else 0:+8.2f} Σ=${sum(pn) if pn else 0:+9.2f} WR={100*sum(1 for x in pn if x>0)/len(pn) if pn else 0:3.0f}% "
          f"pnl日聚类t={round(tp,2) if tp else 'NA'}(日{nd}) 硬止损出口占比={100*worst/len(v):.0f}%")

print("\n" + "=" * 104)
print("③ 高 ATR 组是否更多走 SL_INITIAL (那个 −$4474 出口)? — 波动与尾部的耦合")
print("=" * 104)
for i in range(4):
    v = bk[i]
    if not v: continue
    ers = collections.Counter(x["er"] for x in v)
    pn_er = collections.defaultdict(float)
    for x in v:
        if x["pnl"] is not None: pn_er[x["er"]] += x["pnl"]
    top = sorted(pn_er.items(), key=lambda kv: kv[1])[:2]
    print(f"  {labs[i]:11s} 最亏出口: {[(k, round(x,1), f'n={ers[k]}') for k, x in top]}")

print("\n" + "=" * 104)
print("④ 风险平价核查: ATR 高是否被仓位变小自动抵消 (若是, 则 atr→mfe 不构成可利用杠杆)")
print("=" * 104)
nq = [(s["atr"], s["qty"]*s["ep"]) for s in A if s["qty"] and s["ep"]]
print(f"  Spearman(atr_pct, notional) = {round(spear([x[0] for x in nq],[x[1] for x in nq]),4)}  n={len(nq)}")
nr = [(s["atr"], s["ra"]) for s in A if s["ra"]]
print(f"  Spearman(atr_pct, risk_amount) = {round(spear([x[0] for x in nr],[x[1] for x in nr]),4) if nr else 'NA'}  n={len(nr)}")
for i in range(4):
    v = bk[i]
    if not v: continue
    nt = [x["qty"]*x["ep"] for x in v if x["qty"] and x["ep"]]
    ra = [x["ra"] for x in v if x["ra"]]
    print(f"  {labs[i]:11s} notional中位=${st.median(nt) if nt else 0:,.0f} risk_amount中位=${st.median(ra) if ra else 0:,.1f} "
          f"atr中位={st.median([x['atr'] for x in v]):.4f}")
print("\n  ⇒ 若 notional 随 ATR 下降而 risk_amount 恒定 = 风险平价已把波动中性化")
print("     则「选高波动票」提的是 MFE 的**百分比**, 不是**美元净利** ⇒ 不是杠杆")
