#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""[DEB-20261007 追问 · 阶段5] 决定性检验: 「挂限价等回踩到计划价」可不可行?

为什么这是决定性的 (且不需要拉 bar):
  追价组 = 实际成交价比计划价贵 adverse%。若要「等回踩」, 前提是**价格后来真的回到过计划价**。
  trade_records.mae_pct = 入场后最大不利偏移 ⇒ 对 LONG, 若 mae_pct ≥ adverse, 说明入场后价格
  曾下探到计划价以下 ⇒ 限价单在计划价**可成交**; 反之价格一去不回头 ⇒ 等回踩 = 永远不成交 = 丢单。
  这把「入场价能不能挑」从统计问题变成**可达性**问题 (T29-4 m2m 幻象的正解)。

三个必须并列报的读数 (T22-4: 躲损与放弃缺一不可):
  R1 可达率: 追价组里价格曾回到计划价的占比
  R2 躲损: 可达子集若按计划价成交, 省了多少 (Δ = adverse%, 按 notional 折算 $)
  R3 放弃: 不可达子集 = 这些单等不到回踩 ⇒ 机会成本 (它们的实际 pnl_net 合计)
  R4 反直觉核查: 可达子集 (价格回来过) 的**实际结局**是否比不可达子集更差?
     ⇒ 若是, 则「回踩到了」本身是趋势反转的标记, 等回踩会系统性地只接到坏单 (负选择)
"""
from pathlib import Path as _P
_ROOT = str(_P(__file__).resolve().parents[1])
import sqlite3, json, math, statistics as st, collections
from datetime import datetime

c = sqlite3.connect(f"file:{_ROOT}/data/ftseek_us.db?mode=ro", uri=True)
c.row_factory = sqlite3.Row

inst = {}
for r in c.execute("""SELECT id, decision_id, date, symbol, direction d, entry_price e, atr_pct atr,
                             raw_instruction_json raw, confidence conf
                      FROM playbook_instructions WHERE decision_id IS NOT NULL ORDER BY id"""):
    k = str(r["decision_id"])
    if k not in inst: inst[k] = r

tr = {}
for t in c.execute("SELECT * FROM trade_records WHERE source_type='PLAYBOOK_SIM'"):
    k = str(t["decision_id"] or "")
    if k: tr.setdefault(k, []).append(t)

S = []
skip = collections.Counter()
for k, r in inst.items():
    try: raw = json.loads(r["raw"]) if r["raw"] else {}
    except Exception: raw = {}
    if not isinstance(raw, dict): raw = {}
    pe = raw.get("planned_entry")
    if not (isinstance(pe, (int, float)) and float(pe) > 0): skip["无planned_entry"] += 1; continue
    e = float(r["e"] or 0)
    if e <= 0: skip["无entry_price"] += 1; continue
    if k not in tr: skip["未成交"] += 1; continue
    t = tr[k][0]
    if t["mfe_pct"] is None: skip["无mfe"] += 1; continue
    short = str(r["d"] or "").upper().startswith("S")
    dev = (e - float(pe)) / float(pe) * 100
    adverse = dev if not short else -dev          # >0 = 比计划贵 (对持仓不利)
    mae = float(t["mae_pct"]) if t["mae_pct"] is not None else None
    qty = float(t["quantity"]) if t["quantity"] else None
    S.append(dict(id=r["id"], did=k, date=r["date"], td=t["trade_date"], sym=r["symbol"], short=short,
                  pe=float(pe), e=e, dev=dev, adverse=adverse, mae=mae, mfe=float(t["mfe_pct"]),
                  pnl=float(t["pnl_net"]) if t["pnl_net"] is not None else None,
                  qty=qty, notional=(e*qty if qty else None), er=str(t["exit_reason"] or ""),
                  atr=float(r["atr"]) if r["atr"] else None))

print(f"人群 n={len(S)}   跳过: {dict(skip)}")
chase = [s for s in S if s["adverse"] > 0.05]
cheap = [s for s in S if s["adverse"] < -0.05]
print(f"追价组 (比计划贵 >0.05%) n={len(chase)}   便宜组 n={len(cheap)}   近等组 n={len(S)-len(chase)-len(cheap)}")
print(f"追价幅度 |adverse| 中位={st.median(sorted(abs(s['adverse']) for s in chase)):.4f}% "
      f"p90={sorted(abs(s['adverse']) for s in chase)[int(len(chase)*.9)]:.4f}%")

print("\n" + "=" * 104)
print("R1  可达率: 追价组里, 价格后来真的回到过计划价吗? (mae_pct ≥ adverse)")
print("=" * 104)
have_mae = [s for s in chase if s["mae"] is not None]
print(f"  有 mae_pct 的追价笔 n={len(have_mae)}/{len(chase)}")
reach = [s for s in have_mae if s["mae"] >= s["adverse"]]
unreach = [s for s in have_mae if s["mae"] < s["adverse"]]
print(f"  ✅ 可达 (mae ≥ adverse, 价格曾回到计划价以下) = {len(reach)}/{len(have_mae)} = {100*len(reach)/len(have_mae):.1f}%")
print(f"  ❌ 不可达 (价格一去不回头)              = {len(unreach)}/{len(have_mae)} = {100*len(unreach)/len(have_mae):.1f}%")
print(f"  ⚠️ 口径声明: mae_pct 是**入场后**极值, 不含「入场前那一刻」; 且 mae 为百分比口径,")
print(f"     与 adverse 同口径 (都相对计划/入场价的%), 可直接比较。")

# 按追价幅度分档看可达率 (是否追得越多越等不到)
print("\n  --- 可达率 × 追价幅度分档 (决定「小追可等, 大追必丢」是否成立) ---")
qs = sorted(abs(s["adverse"]) for s in have_mae)
if len(qs) >= 12:
    cuts = [qs[int(len(qs)*p)] for p in (0.33, 0.66)]
    labs = ["小追(<%.2f%%)" % cuts[0], "中追", "大追(>%.2f%%)" % cuts[1]]
    for i, lb in enumerate(labs):
        lo_ = cuts[i-1] if i else -1e9; hi_ = cuts[i] if i < 2 else 1e9
        g = [s for s in have_mae if lo_ < abs(s["adverse"]) <= hi_]
        if not g: continue
        rc = [s for s in g if s["mae"] >= s["adverse"]]
        flag = "" if len(g) >= 30 else "  ⚠️n<30只观察"
        print(f"    {lb:18s} n={len(g):3d} 可达={len(rc):3d} ({100*len(rc)/len(g):3.0f}%) "
              f"|adverse|中位={st.median(sorted(abs(s['adverse']) for s in g)):.3f}%{flag}")

print("\n" + "=" * 104)
print("R2  躲损: 可达子集若按计划价成交, 能省多少? (Δ = adverse% × notional)")
print("=" * 104)
sav = [s["adverse"]/100 * s["notional"] for s in reach if s["notional"]]
if sav:
    sv = sorted(sav)
    print(f"  n={len(sv)} 合计省 ${sum(sv):,.2f}  笔均 ${st.mean(sv):,.2f}  中位 ${st.median(sv):,.2f}  max ${sv[-1]:,.2f}")
    print(f"  占该子集名义总额 ${sum(s['notional'] for s in reach if s['notional']):,.0f} 的 {100*sum(sv)/sum(s['notional'] for s in reach if s['notional']):.3f}%")
    # 与实际总盈亏对照 (T43-1: $ 上限对照系统总盈亏)
    allpnl = sum(s["pnl"] for s in S if s["pnl"] is not None)
    print(f"  对照: 全人群 Σpnl_net = ${allpnl:,.2f}  ⇒ 省额占其 {100*sum(sv)/abs(allpnl) if allpnl else float('nan'):.1f}%")
    print(f"  ⚠️ 但这是**假设按计划价成交且后续路径不变**的乐观上限 — 实际入场价更低会改变 SL/TP 触发点")

print("\n" + "=" * 104)
print("R3  放弃: 不可达子集的机会成本 (T22-4: 不成交=丢单)")
print("=" * 104)
up = [s for s in unreach if s["pnl"] is not None]
if up:
    pn = [s["pnl"] for s in up]
    print(f"  不可达 n={len(up)} 实际 Σpnl_net=${sum(pn):,.2f} 笔均=${st.mean(pn):+,.2f} "
          f"WR={100*sum(1 for x in pn if x>0)/len(pn):.0f}%")
    print(f"  ⇒ 若对这些单挂限价等回踩, 会**丢掉 ${sum(pn):,.2f}** (正=丢利润, 负=躲亏损)")

print("\n" + "=" * 104)
print("R4  🔴 负选择核查: 可达 (价格回来过) 的子集, 实际结局是否更差?")
print("=" * 104)
for nm, g in (("可达(回踩过计划价)", reach), ("不可达(一去不回头)", unreach)):
    pn = [s["pnl"] for s in g if s["pnl"] is not None]
    mf = sorted(s["mfe"] for s in g)
    if not pn:
        print(f"  {nm}: n={len(g)} 无pnl"); continue
    byd = collections.defaultdict(list)
    for s in g:
        if s["pnl"] is not None: byd[s["td"]].append(s["pnl"])
    ds = [st.mean(v) for v in byd.values()]
    sd = st.stdev(ds) if len(ds) > 1 else 0
    t = st.mean(ds)/(sd/math.sqrt(len(ds))) if sd > 1e-12 else None
    flag = "" if len(g) >= 30 else "  ⚠️n<30只观察"
    print(f"  {nm:22s} n={len(g):3d} 日数={len(byd):2d} 笔均pnl=${st.mean(pn):+8.2f} 中位=${st.median(pn):+8.2f} "
          f"WR={100*sum(1 for x in pn if x>0)/len(pn):3.0f}% mfe中位={st.median(mf):.4f}% "
          f"日聚类t={round(t,2) if t else 'NA'}{flag}")
    ers = collections.Counter(s["er"] for s in g)
    print(f"       出口分布 top4: {ers.most_common(4)}")
if reach and unreach:
    a = [s["pnl"] for s in reach if s["pnl"] is not None]
    b = [s["pnl"] for s in unreach if s["pnl"] is not None]
    if a and b:
        d = st.mean(a) - st.mean(b)
        print(f"\n  ⇒ 差 (可达 − 不可达) = ${d:+.2f}/笔")
        print(f"     {'🔴 可达组更差 = 回踩是趋势反转标记 ⇒ 等回踩会系统性只接到坏单 (负选择)' if d < 0 else '可达组更好 ⇒ 等回踩方向正确'}")

print("\n" + "=" * 104)
print("R5  便宜组反向核查: 已经买到比计划便宜的人, 结局更好吗? (这是「限价等回踩」的现成对照)")
print("=" * 104)
for nm, g in (("追价组", chase), ("便宜组", cheap)):
    pn = [s["pnl"] for s in g if s["pnl"] is not None]
    mf = sorted(s["mfe"] for s in g)
    if not pn: continue
    byd = collections.defaultdict(list)
    for s in g:
        if s["pnl"] is not None: byd[s["td"]].append(s["pnl"])
    ds = [st.mean(v) for v in byd.values()]
    sd = st.stdev(ds) if len(ds) > 1 else 0
    t = st.mean(ds)/(sd/math.sqrt(len(ds))) if sd > 1e-12 else None
    print(f"  {nm}: n={len(g):3d} 日数={len(byd):2d} 笔均=${st.mean(pn):+8.2f} 中位=${st.median(pn):+8.2f} "
          f"WR={100*sum(1 for x in pn if x>0)/len(pn):3.0f}% mfe中位={st.median(mf):.4f}% "
          f"死单率={100*sum(1 for x in mf if x<0.3)/len(mf):.0f}% 日聚类t={round(t,2) if t else 'NA'}")
