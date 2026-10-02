"""第十二轮 (最终修正): atr 量纲污染清除后重算

自我更正 (两轮都错):
  · metadata.atr 是**百分数**口径 (中位 7.475 = 7.475%, max 109.43)
  · 门用的 inst.atr_pct 是**比例**口径 (skip_reason 实测 0.0299<0.03 = 2.99%)
    ⇒ 两者不同量纲, 不能用 metadata.atr 精确复刻门
  · atr=0.0000 是缺值哨兵 (n 待查), 第九轮 "SHORT×ATR<5%" 49 笔里混了 15 笔零值
    ⇒ 第九轮那条最强腿读数被污染; 第十一轮②③⑤ 用 <0.03 筛出的正是零值行 ⇒ 作废

本轮: ①零值单列 ②百分数口径正确分档 ③只用剔零值后仍稳的腿给最终范围
"""
import json
import sqlite3
import statistics as st

DB = "/opt/ftseek/FTSeek-US/data/ftseek_us.db"
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
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
           else "reentry" if did.startswith("intraday_reentry")
           else "EXT" if did.startswith("EXT") else "other")
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


print("=== ① atr 缺值/零值诊断 (污染面) ===")
n_null = sum(1 for x in recs if x["atr"] is None)
n_zero = sum(1 for x in recs if x["atr"] == 0)
nz = [x["atr"] for x in recs if x["atr"] not in (None, 0)]
print(f"  n={len(recs)} | atr 为 NULL: {n_null} | atr == 0 (缺值哨兵): {n_zero}")
print(f"  非零 atr: n={len(nz)} min={min(nz):.3f} 中位={st.median(nz):.3f} "
      f"p25={sorted(nz)[len(nz)//4]:.3f} p75={sorted(nz)[3*len(nz)//4]:.3f} max={max(nz):.3f}")
print(f"  ⇒ 口径 = **百分数** (中位 {st.median(nz):.2f}%)")
z = [x for x in recs if x["atr"] == 0]
if z:
    az = A(z)
    print(f"  零值行结局: n={az['n']} 净={az['net']:+.1f} 均={az['avg']:+.2f} "
          f"死率={az['dead']:.0f}% 月份={sorted({x['month'] for x in z})}")
    print(f"  ⇒ 零值行是 7 月旧仓时代缺采集, **不是低波动票** ⇒ 必须排除, 不得当 ATR 档读数")

VALID = [x for x in recs if x["atr"] not in (None, 0)]
print(f"\n  本轮全部读数只用有效 atr 行: n={len(VALID)}")

print("\n=== ② ATR 百分数口径分档 × 方向 (剔零值) ===")
print(f"  {'档':14s} {'方向':6s} {'n':>4s} {'毛':>8s} {'净':>8s} {'均':>7s} {'WR':>4s} "
      f"{'死率':>5s} {'毛bps':>7s} {'IS均':>7s} {'OOS均':>7s} {'剔3亏':>8s} 判定")
for lo, hi, lab in ((0, 3, "ATR<3%"), (3, 5, "3-5%"), (5, 8, "5-8%"),
                    (8, 12, "8-12%"), (12, 20, "12-20%"), (20, 1e9, ">=20%")):
    for d in ("LONG", "SHORT"):
        s2 = [x for x in VALID if x["dir"] == d and lo <= x["atr"] < hi]
        if len(s2) < 8:
            if s2:
                print(f"  {lab:14s} {d:6s} {len(s2):4d}  (n<8 不读)")
            continue
        a = A(s2)
        i_ = A([x for x in s2 if x["t"] < "2026-08-20"])
        o_ = A([x for x in s2 if x["t"] >= "2026-08-20"])
        ia = i_["avg"] if i_ and i_["n"] >= 3 else None
        oa = o_["avg"] if o_ and o_["n"] >= 3 else None
        d3w, d3b = rob(s2)
        if ia is None or oa is None:
            v = "⚠️窗内n不足"
        elif ia > 0 and oa > 0:
            v = "✅双窗正" + ("+剔尾稳" if d3w > 0 and d3b > 0 else "+剔尾翻负🔴")
        elif (ia > 0) != (oa > 0):
            v = "🔴反号"
        else:
            v = "⚠️双负"
        print(f"  {lab:14s} {d:6s} {a['n']:4d} {a['gross']:+8.1f} {a['net']:+8.1f} "
              f"{a['avg']:+7.2f} {a['wr']:3.0f}% {a['dead']:4.0f}% {a['gb']:+7.1f} "
              f"{(ia if ia is not None else 0):+7.2f} {(oa if oa is not None else 0):+7.2f} "
              f"{d3w:+8.1f} {v}")

print("\n=== ③ gap 轴 × 方向 (剔零值 atr 无关, 但同口径重算) ===")
def gband(v):
    if v is None: return "空"
    a = abs(v)
    return "<1%" if a < 1 else "1-3%" if a < 3 else "3-8%" if a < 8 else ">=8%"
print(f"  {'gap档':10s} {'方向':6s} {'n':>4s} {'毛':>8s} {'净':>8s} {'均':>7s} {'WR':>4s} "
      f"{'死率':>5s} {'IS均':>7s} {'OOS均':>7s} {'剔3亏':>8s} {'剔3赚':>8s} 判定")
for gb in ("<1%", "1-3%", "3-8%", ">=8%"):
    for d in ("LONG", "SHORT"):
        s2 = [x for x in recs if x["dir"] == d and gband(x["gap"]) == gb]
        if len(s2) < 8:
            continue
        a = A(s2)
        i_ = A([x for x in s2 if x["t"] < "2026-08-20"])
        o_ = A([x for x in s2 if x["t"] >= "2026-08-20"])
        ia = i_["avg"] if i_ and i_["n"] >= 3 else None
        oa = o_["avg"] if o_ and o_["n"] >= 3 else None
        d3w, d3b = rob(s2)
        if ia is None or oa is None:
            v = "⚠️单窗"
        elif ia > 0 and oa > 0:
            v = "✅双窗正" + ("+剔尾稳" if d3w > 0 and d3b > 0 else "+剔尾翻负🔴")
        elif (ia > 0) != (oa > 0):
            v = "🔴反号"
        else:
            v = "⚠️双负"
        print(f"  {gb:10s} {d:6s} {a['n']:4d} {a['gross']:+8.1f} {a['net']:+8.1f} "
              f"{a['avg']:+7.2f} {a['wr']:3.0f}% {a['dead']:4.0f}% "
              f"{(ia if ia is not None else 0):+7.2f} {(oa if oa is not None else 0):+7.2f} "
              f"{d3w:+8.1f} {d3b:+8.1f} {v}")

print("\n=== ④ 组合: SHORT × gap<1% × ATR有效档 (剔零值后是否仍强) ===")
for lab, pred in (
    ("SHORT gap<1% (全 atr)", lambda x: x["dir"] == "SHORT" and gband(x["gap"]) == "<1%"),
    ("SHORT gap<1% × atr<10%", lambda x: x["dir"] == "SHORT" and gband(x["gap"]) == "<1%"
     and x["atr"] not in (None, 0) and x["atr"] < 10),
    ("SHORT gap<1% × atr>=10%", lambda x: x["dir"] == "SHORT" and gband(x["gap"]) == "<1%"
     and x["atr"] not in (None, 0) and x["atr"] >= 10),
    ("SHORT atr<10% (剔零)", lambda x: x["dir"] == "SHORT" and x["atr"] not in (None, 0) and x["atr"] < 10),
    ("SHORT atr<8% (剔零)", lambda x: x["dir"] == "SHORT" and x["atr"] not in (None, 0) and x["atr"] < 8),
):
    s2 = [x for x in recs if pred(x)]
    if len(s2) < 6:
        print(f"  {lab:28s} n={len(s2)} (不足)")
        continue
    a = A(s2)
    d3w, d3b = rob(s2)
    i_ = A([x for x in s2 if x["t"] < "2026-08-20"])
    o_ = A([x for x in s2 if x["t"] >= "2026-08-20"])
    ms = []
    for m in sorted({x["month"] for x in s2}):
        mm = [x for x in s2 if x["month"] == m]
        if len(mm) >= 2:
            ms.append(f"{m[5:]}:{A(mm)['net']:+.0f}")
    signs = [A([x for x in s2 if x["month"] == m])["net"] > 0
             for m in sorted({x["month"] for x in s2})
             if len([x for x in s2 if x["month"] == m]) >= 2]
    cons = ("✅全正" if signs and all(signs) else "🔴全负" if signs and not any(signs) else "⚠️混合")
    print(f"  {lab:28s} n={a['n']:3d} 毛={a['gross']:+7.1f} 净={a['net']:+7.1f} "
          f"均={a['avg']:+7.2f} WR={a['wr']:3.0f}% 死率={a['dead']:3.0f}% "
          f"剔3亏={d3w:+7.1f} 剔3赚={d3b:+7.1f}")
    print(f"      IS={i_['avg'] if i_ else 0:+.2f}(n{i_['n'] if i_ else 0}) "
          f"OOS={o_['avg'] if o_ else 0:+.2f}(n{o_['n'] if o_ else 0}) | 逐月 {' '.join(ms)} {cons}")

print("\n=== ⑤ 最终减法清单 (只用逐月同号 + 剔尾仍同号的刀) ===")
fee_ok = lambda x: x["price"] >= 26 and x["notional"] >= 4200
KN = {
    "砍 UE·LONG": lambda x: x["src"] == "UE" and x["dir"] == "LONG",
    "砍 不过费用线": lambda x: not fee_ok(x),
    "砍 SHORT×atr>=12%": lambda x: x["dir"] == "SHORT" and x["atr"] not in (None, 0) and x["atr"] >= 12,
    "砍 LONG×atr>=20%": lambda x: x["dir"] == "LONG" and x["atr"] not in (None, 0) and x["atr"] >= 20,
    "砍 LONG×gap>=3%": lambda x: x["dir"] == "LONG" and x["gap"] is not None and abs(x["gap"]) >= 3,
    "砍 SHORT×gap>=8%": lambda x: x["dir"] == "SHORT" and x["gap"] is not None and abs(x["gap"]) >= 8,
}
base = A(recs)
print(f"  基线 n={base['n']} 毛={base['gross']:+.1f} 费={base['fees']:.1f} "
      f"净={base['net']:+.1f} 均={base['avg']:+.2f} 死率={base['dead']:.0f}%")
for name, pred in KN.items():
    s2 = [x for x in recs if pred(x)]
    if len(s2) < 5:
        continue
    a = A(s2)
    ms, signs = [], []
    for m in sorted({x["month"] for x in s2}):
        mm = [x for x in s2 if x["month"] == m]
        if len(mm) >= 3:
            aa = A(mm)
            ms.append(f"{m[5:]}:{aa['net']:+.0f}")
            signs.append(aa["net"] > 0)
    cons = ("✅全负(该砍)" if signs and not any(signs) else
            "🔴全正(不该砍)" if signs and all(signs) else "⚠️混合")
    lesion = "入场(毛负)" if a["gross"] < 0 else "费用(毛正净负)"
    print(f"  {name:20s} n={a['n']:3d} 毛={a['gross']:+8.1f} 费={a['fees']:7.1f} "
          f"净={a['net']:+8.1f} 死率={a['dead']:3.0f}% {lesion:14s} {cons} | {' '.join(ms)}")

print("\n=== ⑥ 综合画像 (只叠加逐月全负的刀) ===")
FINAL = lambda x: (not (x["src"] == "UE" and x["dir"] == "LONG")
                   and fee_ok(x)
                   and not (x["dir"] == "SHORT" and x["atr"] not in (None, 0) and x["atr"] >= 12))
fin = [x for x in recs if FINAL(x)]
a = A(fin)
d3w, d3b = rob(fin)
print(f"  n={a['n']} ({a['n']/base['n']*100:.0f}%流量) 毛={a['gross']:+.1f} 费={a['fees']:.1f} "
      f"净={a['net']:+.1f} 均={a['avg']:+.2f} WR={a['wr']:.0f}% 死率={a['dead']:.0f}% "
      f"毛bps={a['gb']:.1f} 费bps={a['fb']:.1f}")
print(f"  剔最亏3票={d3w:+.1f} ({'✅' if d3w>0 else '🔴'}) 剔最赚3票={d3b:+.1f} ({'✅' if d3b>0 else '🔴'})")
for d in ("LONG", "SHORT"):
    dd = [x for x in fin if x["dir"] == d]
    if dd:
        aa = A(dd)
        print(f"   {d}: n={aa['n']:3d} 毛={aa['gross']:+8.1f} 净={aa['net']:+8.1f} "
              f"均={aa['avg']:+6.2f} WR={aa['wr']:3.0f}% 死率={aa['dead']:3.0f}%")
for lab, p in (("IS ", lambda x: x["t"] < "2026-08-20"), ("OOS", lambda x: x["t"] >= "2026-08-20")):
    s3 = [x for x in fin if p(x)]
    b3 = [x for x in recs if p(x)]
    if len(s3) >= 5:
        aa, ab = A(s3), A(b3)
        print(f"   {lab}: n={aa['n']:3d} 净={aa['net']:+8.1f} 均={aa['avg']:+6.2f} "
              f"WR={aa['wr']:3.0f}% | 基线均={ab['avg']:+6.2f} Δ={aa['avg']-ab['avg']:+6.2f} "
              f"{'✅' if aa['avg']>ab['avg'] else '🔴'}")
ms = []
for m in sorted({x["month"] for x in fin}):
    mm = [x for x in fin if x["month"] == m]
    bb = [x for x in recs if x["month"] == m]
    if len(mm) < 3:
        ms.append(f"{m[5:]}:n{len(mm)}")
        continue
    ms.append(f"{m[5:]}:{A(mm)['net']:+.0f}(基{A(bb)['net']:+.0f})")
print(f"   逐月: {' '.join(ms)}")
days = {}
for x in fin:
    days.setdefault(x["day"], []).append(x)
daily = [sum(y["net"] for y in v) for v in days.values()]
dg = [sum(y["gross"] for y in v) for v in days.values()]
if len(daily) > 3 and st.pstdev(daily) > 0:
    print(f"   日聚类: 交易日={len(daily)} 日净均={st.mean(daily):+.2f} "
          f"t净={st.mean(daily)/(st.pstdev(daily)/len(daily)**.5):+.2f} "
          f"t毛={st.mean(dg)/(st.pstdev(dg)/len(dg)**.5) if st.pstdev(dg)>0 else 0:+.2f}")
cut = [x for x in recs if not FINAL(x)]
ac = A(cut)
win = [x for x in cut if x["net"] > 0]
print(f"  砍掉 n={ac['n']} ({ac['n']/base['n']*100:.0f}%) 毛={ac['gross']:+.1f} "
      f"费={ac['fees']:.1f} 净={ac['net']:+.1f} | 其中赢单 {len(win)} 笔 +{sum(x['net'] for x in win):.0f}")
c.close()
