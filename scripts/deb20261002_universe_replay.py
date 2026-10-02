"""第十八轮 (最终定稿): 撤掉 UE·LONG 刀后的推荐范围

撤回理由 (第十七轮):
  · P3 (9/29 RFC-117 重开 LONG 后) UE·LONG 已平仓成交 n=0 ⇒ 无数据能否定用户裁决
  · UE·LONG 亏损 84% 落在"不过费用线"侧 (−708 vs 过线 −133) ⇒ 费用线已覆盖
  · 过费用线子集 OOS 均 +2.35 / 死率 17% (IS −32.25) ⇒ 趋势在改善, 不是结构毒
  · 与用户 9/29 裁决正面冲突, 且我 10/2 才刚登记"UE 转正前必须复核"

保留的刀 (只用逐月同号 + 剔尾仍同号 + 双窗同向):
  K1 费用线 (价>=$26 且 名义>=$4200)  — 逐月全负, 最大杠杆
  K2 SHORT 侧 |gap|>=8% 剔除          — 逐月全负 (9月−355)
  K3 SHORT 侧 原始ATR>=8% 剔除        — 单调轴, >=8% 段 −535
  K4 LONG 侧 |gap| 3-8% 剔除          — 死率39.2% 净−1368 (逐月混合, 需标注)
"""
import json
import sqlite3
import statistics as st

BASE = "/opt/ftseek/FTSeek-US"
c = sqlite3.connect(f"file:{BASE}/data/ftseek_us.db?mode=ro", uri=True)
c.row_factory = sqlite3.Row

rows = c.execute(
    "SELECT symbol, direction, entry_time, entry_price, quantity, decision_id, "
    " metadata_json, mfe_pct, pnl_net, realized_pnl, fees_est "
    "FROM trade_records WHERE entry_time>='2026-06-27' AND exit_time IS NOT NULL "
    " AND pnl_net IS NOT NULL").fetchall()

recs = []
for r in rows:
    try:
        md = json.loads(r["metadata_json"]) if r["metadata_json"] else {}
    except Exception:
        md = {}
    gap = md.get("entry_gap") or {}
    did = r["decision_id"] or ""
    src = ("DA" if did.startswith("DA-") else "UE" if did.startswith("universe_expansion")
           else "reentry" if did.startswith("intraday_reentry") else "other")
    recs.append({
        "sym": r["symbol"], "dir": r["direction"], "t": r["entry_time"] or "",
        "day": (r["entry_time"] or "")[:10], "month": (r["entry_time"] or "")[:7],
        "price": r["entry_price"] or 0,
        "notional": (r["entry_price"] or 0) * (r["quantity"] or 0), "src": src,
        "gap": gap.get("aligned_gap_pct", gap.get("gap_pct")), "atr": md.get("atr"),
        "dead": 1 if (r["mfe_pct"] or 0) < 0.3 else 0,
        "net": r["pnl_net"] or 0, "gross": r["realized_pnl"] or 0, "fees": r["fees_est"] or 0,
    })


def A(g):
    n = len(g)
    if not n:
        return None
    net = sum(x["net"] for x in g)
    gross = sum(x["gross"] for x in g)
    fees = sum(x["fees"] for x in g)
    nt = sum(x["notional"] for x in g)
    return dict(n=n, net=net, gross=gross, fees=fees, avg=net / n,
                wr=sum(1 for x in g if x["net"] > 0) / n * 100,
                dead=sum(x["dead"] for x in g) / n * 100,
                gb=gross / nt * 1e4 if nt else 0, fb=fees / nt * 1e4 if nt else 0)


def rob(g):
    syms = {}
    for x in g:
        syms[x["sym"]] = syms.get(x["sym"], 0) + x["net"]
    rk = sorted(syms.items(), key=lambda z: z[1])
    tot = sum(x["net"] for x in g)
    return tot - sum(v for _, v in rk[:3]), tot - sum(v for _, v in rk[-3:])


fee_ok = lambda x: x["price"] >= 26 and x["notional"] >= 4200
atr_ok = lambda x: x["atr"] not in (None, 0)
KN = {
    "K1 费用线": fee_ok,
    "K2 SHORT剔|gap|>=8%": lambda x: not (x["dir"] == "SHORT" and x["gap"] is not None
                                          and abs(x["gap"]) >= 8),
    "K3 SHORT剔原始ATR>=8%": lambda x: not (x["dir"] == "SHORT" and atr_ok(x) and x["atr"] >= 8),
    "K4 LONG剔|gap|3-8%": lambda x: not (x["dir"] == "LONG" and x["gap"] is not None
                                         and 3 <= abs(x["gap"]) < 8),
}

print("=== 逐刀独立效果 (基线 → 单刀) ===")
base = A(recs)
print(f"  基线       n={base['n']:3d} 毛={base['gross']:+8.1f} 费={base['fees']:7.1f} "
      f"净={base['net']:+8.1f} 均={base['avg']:+6.2f} 死率={base['dead']:4.1f}%")
for name, p in KN.items():
    g = [x for x in recs if p(x)]
    a = A(g)
    d3w, d3b = rob(g)
    print(f"  {name:20s} n={a['n']:3d} 毛={a['gross']:+8.1f} 费={a['fees']:7.1f} "
          f"净={a['net']:+8.1f} 均={a['avg']:+6.2f} 死率={a['dead']:4.1f}% "
          f"Δ净={a['net']-base['net']:+8.1f} Δ死率={a['dead']-base['dead']:+5.1f}pp "
          f"剔3亏={d3w:+7.0f}{'✅' if d3w>0 else '🔴'}")

print("\n=== 叠加效果 (K1 → K1+K2 → +K3 → +K4) ===")
order = ["K1 费用线", "K2 SHORT剔|gap|>=8%", "K3 SHORT剔原始ATR>=8%", "K4 LONG剔|gap|3-8%"]
cur = recs
for i, name in enumerate(order):
    cur = [x for x in cur if KN[name](x)]
    a = A(cur)
    d3w, d3b = rob(cur)
    print(f"  {'+'.join(order[:i+1])[:56]:56s}")
    print(f"      n={a['n']:3d} 毛={a['gross']:+8.1f} 费={a['fees']:7.1f} 净={a['net']:+8.1f} "
          f"均={a['avg']:+6.2f} WR={a['wr']:3.0f}% 死率={a['dead']:4.1f}% "
          f"毛bps={a['gb']:+5.1f} 费bps={a['fb']:4.1f} 剔3亏={d3w:+7.0f}{'✅' if d3w>0 else '🔴'} "
          f"剔3赚={d3b:+7.0f}{'✅' if d3b>0 else '🔴'}")

FINAL = lambda x: all(p(x) for p in KN.values())
fin = [x for x in recs if FINAL(x)]
a = A(fin)
print("\n=== 最终推荐范围 (K1+K2+K3+K4) 完整读数 ===")
print(f"  n={a['n']} ({a['n']/base['n']*100:.0f}%流量) 毛={a['gross']:+.1f} 费={a['fees']:.1f} "
      f"净={a['net']:+.1f} 均={a['avg']:+.2f} WR={a['wr']:.0f}% 死率={a['dead']:.1f}%")
print(f"  vs 基线: Δ净={a['net']-base['net']:+.1f} Δ死率={a['dead']-base['dead']:+.1f}pp "
      f"费bps {base['fb']:.1f}→{a['fb']:.1f}")
for d in ("LONG", "SHORT"):
    dd = [x for x in fin if x["dir"] == d]
    if dd:
        aa = A(dd)
        print(f"   {d}: n={aa['n']:3d} ({aa['n']/a['n']*100:.0f}%) 毛={aa['gross']:+8.1f} "
              f"净={aa['net']:+8.1f} 均={aa['avg']:+6.2f} WR={aa['wr']:3.0f}% 死率={aa['dead']:3.0f}%")
for lab, p in (("IS <8/20", lambda x: x["t"] < "2026-08-20"),
               ("OOS >=8/20", lambda x: x["t"] >= "2026-08-20")):
    s3, b3 = [x for x in fin if p(x)], [x for x in recs if p(x)]
    if len(s3) >= 5:
        aa, ab = A(s3), A(b3)
        print(f"   {lab:12s} n={aa['n']:3d} 净={aa['net']:+8.1f} 均={aa['avg']:+6.2f} "
              f"死率={aa['dead']:3.0f}% | 基线均={ab['avg']:+6.2f} 死率={ab['dead']:3.0f}% "
              f"⇒ Δ均={aa['avg']-ab['avg']:+6.2f} Δ死率={aa['dead']-ab['dead']:+5.1f}pp "
              f"{'✅' if aa['avg']>ab['avg'] else '🔴'}")
ms = []
signs = []
for m in sorted({x["month"] for x in fin}):
    mm = [x for x in fin if x["month"] == m]
    bb = [x for x in recs if x["month"] == m]
    if len(mm) < 3:
        ms.append(f"{m[5:]}:n{len(mm)}")
        continue
    aa = A(mm)
    ms.append(f"{m[5:]}:{aa['net']:+.0f}/死{aa['dead']:.0f}%(基{A(bb)['net']:+.0f}/{A(bb)['dead']:.0f}%)")
    signs.append(aa["net"] > A(bb)["net"])
print(f"   逐月: {' '.join(ms)}")
print(f"   逐月改善率: {sum(signs)}/{len(signs)} "
      f"{'✅全月改善' if signs and all(signs) else '⚠️部分月'}")
days = {}
for x in fin:
    days.setdefault(x["day"], []).append(x)
daily = [sum(y["net"] for y in v) for v in days.values()]
dg = [sum(y["gross"] for y in v) for v in days.values()]
if len(daily) > 3 and st.pstdev(daily) > 0:
    tn = st.mean(daily) / (st.pstdev(daily) / len(daily) ** .5)
    tg = st.mean(dg) / (st.pstdev(dg) / len(dg) ** .5) if st.pstdev(dg) > 0 else 0
    print(f"   日聚类: 交易日={len(daily)} 日净均={st.mean(daily):+.2f} t净={tn:+.2f} t毛={tg:+.2f} "
          f"(t毛>2 ⇒ 毛利真实, 非单笔主导)")
cut = [x for x in recs if not FINAL(x)]
ac = A(cut)
win = [x for x in cut if x["net"] > 0]
print(f"   砍掉 n={ac['n']} ({ac['n']/base['n']*100:.0f}%) 毛={ac['gross']:+.1f} "
      f"费={ac['fees']:.1f} 净={ac['net']:+.1f} | 其中赢单 {len(win)} 笔 +{sum(x['net'] for x in win):.0f} "
      f"(机会成本上限)")

print("\n=== 各刀砍掉的流量画像 (给落地写阈值用) ===")
for name, p in KN.items():
    g = [x for x in recs if not p(x)]
    if not g:
        continue
    ag = A(g)
    print(f"  {name:20s} 砍 n={ag['n']:3d} 毛={ag['gross']:+8.1f} 净={ag['net']:+8.1f} "
          f"死率={ag['dead']:3.0f}% 费bps={ag['fb']:5.1f}")
    if name == "K1 费用线":
        for lab, pp in (("价<$26", lambda x: x["price"] < 26),
                        ("价>=$26但名义<$4.2k", lambda x: x["price"] >= 26 and x["notional"] < 4200)):
            s2 = [x for x in g if pp(x)]
            if s2:
                a2 = A(s2)
                print(f"      {lab:20s} n={a2['n']:3d} 净={a2['net']:+8.1f} 费bps={a2['fb']:5.1f}")

print("\n=== SHORT 侧最终画像 (用户核心原则: 必须有真空头产出) ===")
sh = [x for x in fin if x["dir"] == "SHORT"]
if sh:
    a2 = A(sh)
    d3w, d3b = rob(sh)
    print(f"  n={a2['n']} 毛={a2['gross']:+.1f} 净={a2['net']:+.1f} 均={a2['avg']:+.2f} "
          f"WR={a2['wr']:.0f}% 死率={a2['dead']:.0f}% 剔3亏={d3w:+.1f} 剔3赚={d3b:+.1f}")
    ms = []
    for m in sorted({x["month"] for x in sh}):
        mm = [x for x in sh if x["month"] == m]
        if len(mm) >= 2:
            ms.append(f"{m[5:]}:n{len(mm)}/{A(mm)['net']:+.0f}")
    print(f"  逐月: {' '.join(ms)}")
    print(f"  标的清单: {sorted({x['sym'] for x in sh})}")
print("\n=== 最优子腿: SHORT × 原始ATR<3% × |gap|<1% (交集) ===")
best = [x for x in recs if x["dir"] == "SHORT" and atr_ok(x) and x["atr"] < 3
        and x["gap"] is not None and abs(x["gap"]) < 1]
if best:
    ab = A(best)
    d3w, d3b = rob(best)
    print(f"  n={ab['n']} 毛={ab['gross']:+.1f} 净={ab['net']:+.1f} 均={ab['avg']:+.2f} "
          f"WR={ab['wr']:.0f}% **死率={ab['dead']:.0f}%** (基线SHORT死率28.3%)")
    print(f"  剔3亏={d3w:+.1f}{'✅' if d3w>0 else '🔴'} 剔3赚={d3b:+.1f}{'✅' if d3b>0 else '🔴'} "
          f"标的={[x['sym'] for x in sorted(best, key=lambda z: z['t'])]}")
    ms = []
    for m in sorted({x["month"] for x in best}):
        mm = [x for x in best if x["month"] == m]
        ms.append(f"{m[5:]}:n{len(mm)}/{sum(y['net'] for y in mm):+.0f}")
    print(f"  逐月: {' '.join(ms)}")
    post = [x for x in best if x["t"][:10] >= "2026-07-24"]
    print(f"  low_atr_gate 上线(7/24)后: n={len(post)} 净={sum(x['net'] for x in post):+.1f} "
          f"⇒ 该腿活着 (止损口径 atr_pct 与原始 atr 不同物, 门未拦)")
c.close()
