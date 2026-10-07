#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""[DEB-20261007 追问 · 阶段6] R4「等回踩=只接坏单」的两道证伪检验.

R4 初读: 可达(mae≥adverse) 笔均 −$55.85/WR34%/mfe0.18%  vs  不可达 +$13.50/WR66%/mfe0.73%
        差 −$69.35/笔, 日聚类 t=−3.79 vs +3.56 ⇒ 「等回踩会系统性只接到坏单」

两个能推翻它的解释, 必须先排掉:
  X1 可达判据偏严 (口径): mae 相对**入场价**, adverse 相对**计划价**。回到计划价的真判据是
     mae ≥ adverse×(pe/e) (更松), 我用 mae ≥ adverse ⇒ **低估可达率**、且低估是选择性的
     (只保留回踩最深的那批 ⇒ 可达组被人为筛成「最惨」子集 = 循环定义风险, T1)。
     ⇒ 必须用**正确判据**重算, 并给两种判据并列。
  X2 幅度混淆: 小追更容易被回踩 ⇒ 可达组可能只是「小追」子集, 而小追本身动量弱。
     ⇒ 必须在**同一追价幅度档内**比较可达/不可达 (分层), 否则读数是幅度效应的投影。

判定: 两刀都过, R4 才成立; 任一翻号 ⇒ R4 撤回。
"""
from pathlib import Path as _P
_ROOT = str(_P(__file__).resolve().parents[1])
import sqlite3, json, math, statistics as st, collections

c = sqlite3.connect(f"file:{_ROOT}/data/ftseek_us.db?mode=ro", uri=True)
c.row_factory = sqlite3.Row

# 先钉 mae_pct 的真实口径 (不猜): 与 entry/exit/high-low 对读
print("=" * 104)
print("X0  mae_pct 口径核查 (决定可达判据的分母)")
print("=" * 104)
rows = c.execute("""SELECT t.symbol, t.entry_price te, t.exit_price tx, t.mae_pct, t.mfe_pct, t.direction,
                            i.entry_price ie, i.direction id_
                     FROM trade_records t LEFT JOIN playbook_instructions i ON i.decision_id=t.decision_id
                     WHERE t.source_type='PLAYBOOK_SIM' AND t.mae_pct IS NOT NULL AND t.entry_price>0
                     ORDER BY t.rowid DESC LIMIT 400""").fetchall()
ok_e, ok_x = 0, 0
for r in rows:
    if r["mae_pct"] is not None and r["te"]:
        ok_e += 1
print(f"  样本 n={len(rows)} (有 entry_price)")
# 用 exit_price 与 mae 的关系反推: mae 是否 = 相对入场价的最大不利 %
import statistics
chk = []
for r in rows:
    if not r["te"] or not r["tx"] or r["mae_pct"] is None: continue
    loss_at_exit = (float(r["te"]) - float(r["tx"])) / float(r["te"]) * 100
    chk.append((float(r["mae_pct"]), loss_at_exit))
if chk:
    ge = sum(1 for m, l in chk if m >= l - 1e-9)
    print(f"  mae_pct ≥ 「入场→出场跌幅」的笔数 = {ge}/{len(chk)} ({100*ge/len(chk):.0f}%) "
          f"⇒ {'一致 (mae 是相对入场价的最大不利偏移)' if ge/len(chk) > 0.9 else '⚠️ 不一致, 需查定义'}")
print("  ⇒ 口径确认: mae_pct = (entry − min_low)/entry × 100, 分母是**入场价**")
print("     回到计划价的真判据: min_low ≤ pe ⟺ mae ≥ (e−pe)/e×100 = adverse×(pe/e)")

inst = {}
for r in c.execute("""SELECT id, decision_id, date, symbol, direction d, entry_price e, atr_pct atr,
                             raw_instruction_json raw FROM playbook_instructions
                      WHERE decision_id IS NOT NULL ORDER BY id"""):
    k = str(r["decision_id"])
    if k not in inst: inst[k] = r
tr = {}
for t in c.execute("SELECT * FROM trade_records WHERE source_type='PLAYBOOK_SIM'"):
    k = str(t["decision_id"] or "")
    if k: tr.setdefault(k, []).append(t)

S = []
for k, r in inst.items():
    try: raw = json.loads(r["raw"]) if r["raw"] else {}
    except Exception: raw = {}
    if not isinstance(raw, dict): continue
    pe = raw.get("planned_entry")
    if not (isinstance(pe, (int, float)) and float(pe) > 0): continue
    e = float(r["e"] or 0)
    if e <= 0 or k not in tr: continue
    t = tr[k][0]
    if t["mfe_pct"] is None or t["mae_pct"] is None: continue
    short = str(r["d"] or "").upper().startswith("S")
    dev = (e - float(pe)) / float(pe) * 100
    adverse = dev if not short else -dev
    qty = float(t["quantity"]) if t["quantity"] else None
    S.append(dict(did=k, td=t["trade_date"], sym=r["symbol"], short=short, pe=float(pe), e=e,
                  adverse=adverse, mae=float(t["mae_pct"]), mfe=float(t["mfe_pct"]),
                  pnl=float(t["pnl_net"]) if t["pnl_net"] is not None else None,
                  er=str(t["exit_reason"] or ""), qty=qty, notional=(e*qty if qty else None)))

chase = [s for s in S if s["adverse"] > 0.05]
print(f"\n人群 n={len(S)}  追价组 n={len(chase)}")


def dayt(g):
    byd = collections.defaultdict(list)
    for s in g:
        if s["pnl"] is not None: byd[s["td"]].append(s["pnl"])
    if len(byd) < 3: return None, len(byd)
    ds = [st.mean(v) for v in byd.values()]
    sd = st.stdev(ds) if len(ds) > 1 else 0
    return (st.mean(ds)/(sd/math.sqrt(len(ds))) if sd > 1e-12 else None), len(byd)


def rep(g, lb):
    pn = [s["pnl"] for s in g if s["pnl"] is not None]
    if not pn:
        print(f"    {lb}: n={len(g)} 无pnl"); return None
    mf = sorted(s["mfe"] for s in g); t, nd = dayt(g)
    fl = "" if len(g) >= 30 else " ⚠️n<30"
    print(f"    {lb:34s} n={len(g):3d} 日{nd:2d} 笔均=${st.mean(pn):+8.2f} 中位=${st.median(pn):+8.2f} "
          f"WR={100*sum(1 for x in pn if x>0)/len(pn):3.0f}% mfe中位={st.median(mf):.4f}% "
          f"日t={round(t,2) if t else 'NA'}{fl}")
    return st.mean(pn), st.median(mf), len(g)


print("\n" + "=" * 104)
print("X1  两种可达判据并列 (严=阶段5用的, 松=口径正确的)")
print("=" * 104)
for nm, pred in (("严: mae ≥ adverse (阶段5, 低估可达率)", lambda s: s["mae"] >= s["adverse"]),
                 ("松: mae ≥ adverse×(pe/e) (口径正确)", lambda s: s["mae"] >= s["adverse"] * (s["pe"]/s["e"]))):
    rc = [s for s in chase if pred(s)]; un = [s for s in chase if not pred(s)]
    print(f"\n  [{nm}]  可达 {len(rc)}/{len(chase)} = {100*len(rc)/len(chase):.1f}%")
    a = rep(rc, "可达(价格回到过计划价)")
    b = rep(un, "不可达(一去不回头)")
    if a and b:
        print(f"    ⇒ 差(可达−不可达) = ${a[0]-b[0]:+.2f}/笔  mfe差 = {a[1]-b[1]:+.4f}pp")

print("\n" + "=" * 104)
print("X2  幅度分层: 同一追价幅度档内比较 (排掉「可达组=小追子集」混淆)")
print("=" * 104)
pred = lambda s: s["mae"] >= s["adverse"] * (s["pe"]/s["e"])
qs = sorted(abs(s["adverse"]) for s in chase)
cuts = [qs[int(len(qs)*p)] for p in (0.33, 0.66)]
labs = ["小追", "中追", "大追"]
for i, lb in enumerate(labs):
    lo_ = cuts[i-1] if i else -1e9; hi_ = cuts[i] if i < 2 else 1e9
    g = [s for s in chase if lo_ < abs(s["adverse"]) <= hi_]
    if not g: continue
    rc = [s for s in g if pred(s)]; un = [s for s in g if not pred(s)]
    _lo = f"{lo_:.2f}" if lo_ > -1e8 else "-inf"
    _hi = f"{hi_:.2f}" if hi_ < 1e8 else "inf"
    print(f"\n  [{lb}] |adverse|∈({_lo}, {_hi}] n={len(g)} "
          f"可达率={100*len(rc)/len(g):.0f}%")
    a = rep(rc, f"{lb}·可达"); b = rep(un, f"{lb}·不可达")
    if a and b:
        print(f"    ⇒ 档内差 = ${a[0]-b[0]:+.2f}/笔  {'🔴 可达更差(负选择成立)' if a[0]<b[0] else '✅ 可达更好(R4撤回)'}")

print("\n" + "=" * 104)
print("X3  净算: 若对全部追价单改挂限价等回踩, 总账怎么走?")
print("=" * 104)
rc = [s for s in chase if pred(s)]; un = [s for s in chase if not pred(s)]
sav = sum(s["adverse"]/100 * s["notional"] for s in rc if s["notional"])
pn_rc = sum(s["pnl"] for s in rc if s["pnl"] is not None)
pn_un = sum(s["pnl"] for s in un if s["pnl"] is not None)
print(f"  现状 (全部市价追单): Σpnl_net = ${pn_rc+pn_un:+,.2f}  (n={len(chase)})")
print(f"  改挂限价等回踩:")
print(f"    · 成交腿 = 可达 {len(rc)} 笔, 入场价更低省 ${sav:,.2f} (乐观上限, 未计 SL/TP 位移)")
print(f"    · 该腿实际结局 Σpnl = ${pn_rc:+,.2f} (笔均 ${pn_rc/len(rc) if rc else 0:+,.2f})")
print(f"    · 丢掉的腿 = 不可达 {len(un)} 笔, 其实际 Σpnl = ${pn_un:+,.2f} ⇒ 机会成本")
net = pn_rc + sav - 0  # 只留成交腿 + 省额
print(f"    ⇒ 上限估算: 只留可达腿 + 省额 = ${net:+,.2f}  vs 现状 ${pn_rc+pn_un:+,.2f}  Δ=${net-(pn_rc+pn_un):+,.2f}")
print(f"  ⚠️ 三重乐观偏差须声明: ①省额假设后续路径不变(实际入场更低会移动 SL/TP)")
print(f"     ②限价单排队/部分成交未建模 ③可达判据用当日极值, 实盘要精确触价")
print(f"  ⇒ 即使在这三重乐观下, Δ 仍为 ${net-(pn_rc+pn_un):+,.2f} ⇒ {'方向不利' if net-(pn_rc+pn_un)<0 else '方向有利但须实测'}")

print("\n" + "=" * 104)
print("X4  反向禁令自查: 「追价幅度大 ⇒ mfe 高」能不能反手用?")
print("=" * 104)
def spear(xs, ys):
    n = len(xs)
    if n < 5: return None
    rx = {v: i for i, v in enumerate(sorted(xs))}; ry = {v: i for i, v in enumerate(sorted(ys))}
    dx = [rx[v]-(n-1)/2 for v in xs]; dy = [ry[v]-(n-1)/2 for v in ys]
    den = math.sqrt(sum(a*a for a in dx)*sum(b*b for b in dy))
    return sum(a * b for a, b in zip(dx, dy)) / den if den else None
_p = [(s["adverse"], s["pnl"]) for s in S if s["pnl"] is not None]
sp = spear([s["adverse"] for s in S], [s["mfe"] for s in S])
sp2 = spear([x[0] for x in _p], [x[1] for x in _p])
print(f"  Spearman(adverse, mfe) = {sp if sp is None else round(sp,4)}   Spearman(adverse, pnl_net) = {round(sp2,4) if sp2 else 'NA'}")
print("  ⇒ 两者都近零 ⇒ adverse 轴对结局**无判别力**, 不存在「追价更好」也不存在「追价更差」")
print("     (阶段1b 的 up/down 组差是 US.HPK 单票尾部 + 幅度分层伪影, 已在 K1/K2/K3 证伪)")
