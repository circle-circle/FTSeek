#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""[DEB-20261007 追问 · 阶段2] 「追价买贵 → MFE 更低/死单更多」的稳健性五刀.

阶段1b 发现 (口径澄清, 必须先说清):
  - reprice_audit.reprice_from/to **不是**「计划价→执行价」, 而是盘中**最后一次微调**
    (|dev|中位 0.44%, 且 entry_price==reprice_to 156/156) ⇒ 不能用它回答用户的问题。
  - 真正的全程位移 = raw.planned_entry → DB entry_price: |dev|中位 **1.0363%**, p90 3.82%, n=645
    (与 DEB E18 的 1.0101% 同量级 ⇒ E18 口径正确, 阶段1 的 A 人群测的是另一个东西)。
  - 时刻语义: 指令生成→reprice 中位 **67 分钟**, reprice→执行 **0.1 分钟** ⇒ 市价追单。

阶段1b 的初读信号: 追价组 mfe 中位 0.497% / 死单率 25% vs 便宜组 0.798% / 8%。
但 pnl_net 均值被 **US.HPK 单票**主导 (两组最亏3笔全是 HPK) ⇒ 五刀验它是不是共线/尾部伪影。

五刀 (backtest-rule-replay T18/T8/T13/T39/T24):
  K1 剔单票: 去掉 HPK 后 mfe/死单率差是否仍在
  K2 分档单调性: 按 |dev| 分 5 档看 mfe/死单率是否单调 (非单调 = 轴不是真驱动)
  K3 同日截面市场调整: 同日成交对照的 mfe 作基准, 算超额 (防 regime 漂移伪装)
  K4 IS/OOS 对半: 两窗同号 + 日聚类 t
  K5 混淆变量: dev 与 ATR%/confidence/vol_bucket 的相关性 (若 dev 只是高波动的代理 ⇒ 共线)
"""
from pathlib import Path as _P
_ROOT = str(_P(__file__).resolve().parents[1])
import sqlite3, json, math, statistics as st, collections

c = sqlite3.connect(f"file:{_ROOT}/data/ftseek_us.db?mode=ro", uri=True)
c.row_factory = sqlite3.Row

rows = c.execute("""SELECT id, date, symbol, direction d, entry_price e, entry_low lo, entry_high hi,
                           execution_price xp, executed x, raw_instruction_json raw, metadata_json md,
                           atr_pct atr, vol_bucket vb, confidence conf, decision_id did,
                           execution_time xt, created_at ca
                    FROM playbook_instructions ORDER BY id""").fetchall()

tr = {}
for t in c.execute("SELECT * FROM trade_records WHERE source_type='PLAYBOOK_SIM'"):
    k = str(t["decision_id"] or "")
    if k:
        tr.setdefault(k, []).append(t)

# ── 构造人群: 计划价→执行价 位移 (口径 B, 权威全程位移) + join 结局 ──
S = []
for r in rows:
    try: raw = json.loads(r["raw"]) if r["raw"] else {}
    except Exception: raw = {}
    if not isinstance(raw, dict): continue
    pe = raw.get("planned_entry")
    if not (isinstance(pe, (int, float)) and float(pe) > 0): continue
    e = float(r["e"] or 0)
    if e <= 0: continue
    did = str(r["did"] or "")
    if did not in tr: continue
    t = tr[did][0]
    if t["mfe_pct"] is None: continue
    dev = (e - float(pe)) / float(pe) * 100          # >0 = 买得比计划贵 (LONG 口径)
    short = str(r["d"] or "").upper().startswith("S")
    # 方向对齐: SHORT 时买贵=价格下跌=对空头有利 ⇒ 取「对持仓不利」的符号
    adverse = dev if not short else -dev
    S.append(dict(id=r["id"], date=r["date"], sym=r["symbol"], short=short, pe=float(pe), e=e,
                  dev=dev, adverse=adverse, atr=float(r["atr"]) if r["atr"] else None,
                  conf=float(r["conf"]) if r["conf"] else None, vb=r["vb"],
                  mfe=float(t["mfe_pct"]), pnl=float(t["pnl_net"]) if t["pnl_net"] is not None else None,
                  td=t["trade_date"], er=t["exit_reason"], did=did,
                  cap=float(raw["chase_cap_price"]) if isinstance(raw.get("chase_cap_price"), (int, float)) else None,
                  xt=r["xt"]))

print(f"人群 n={len(S)}  (有 planned_entry ∧ 有成交 ∧ 有 mfe_pct)")
if not S:
    raise SystemExit("空人群")
adv = [s["adverse"] for s in S]
aa = sorted(abs(x) for x in adv)
print(f"「不利位移」adverse = LONG:(exec−plan)/plan, SHORT:反向")
print(f"  带符号: 中位={st.median(adv):+.4f}% 均值={st.mean(adv):+.4f}%  不利(>0)占比={100*sum(1 for x in adv if x>0)/len(adv):.1f}%")
print(f"  |adverse|: 中位={st.median(aa):.4f}% p90={aa[int(len(aa)*.9)]:.4f}% max={aa[-1]:.4f}%")
print(f"  按方向: LONG n={sum(1 for s in S if not s['short'])} SHORT n={sum(1 for s in S if s['short'])}")


def grp_report(g, label):
    if not g:
        print(f"  {label}: n=0"); return None
    mf = sorted(s["mfe"] for s in g)
    n = len(mf)
    dead = 100 * sum(1 for x in mf if x < 0.3) / n
    meat = 100 * sum(1 for x in mf if x >= 1.5) / n
    # 日聚类 t on mfe
    byd = collections.defaultdict(list)
    for s in g: byd[s["td"]].append(s["mfe"])
    ds = [st.mean(v) for v in byd.values()]
    t = None
    if len(ds) >= 3:
        sd = st.stdev(ds)
        t = st.mean(ds) / (sd / math.sqrt(len(ds))) if sd > 1e-12 else None
    pn = [s["pnl"] for s in g if s["pnl"] is not None]
    syms = collections.Counter(s["sym"] for s in g)
    print(f"  {label}: n={n:3d} 日数={len(byd):3d} | mfe 中位={st.median(mf):.4f}% 均值={st.mean(mf):+.4f}% "
          f"日聚类t={round(t,2) if t else 'NA'} | 死单率(<0.3%)={dead:.0f}% 有肉率(≥1.5%)={meat:.0f}% "
          f"| pnl均值=${st.mean(pn):+.2f}" if pn else f"  {label}: n={n} mfe中位={st.median(mf):.4f}%")
    print(f"       top票占比: {syms.most_common(2)}")
    return dict(n=n, mfe_med=st.median(mf), mfe_mean=st.mean(mf), dead=dead, meat=meat, t=t, days=len(byd))


print("\n" + "=" * 104)
print("K1  剔单票 (US.HPK 主导两组尾部) — 全样本 vs 剔 HPK")
print("=" * 104)
mid = st.median(adv)
up = [s for s in S if s["adverse"] > 0.05]
dn = [s for s in S if s["adverse"] < -0.05]
print("  [全样本]")
ru = grp_report(up, "追价买贵 (adverse>0.05%)")
rd = grp_report(dn, "买得更便宜 (adverse<−0.05%)")
print("  [剔 US.HPK]")
ru2 = grp_report([s for s in up if s["sym"] != "US.HPK"], "追价买贵")
rd2 = grp_report([s for s in dn if s["sym"] != "US.HPK"], "买得更便宜")
if ru and rd and ru2 and rd2:
    print(f"\n  ⇒ 剔 HPK 前后: mfe中位差 {ru['mfe_med']-rd['mfe_med']:+.4f}pp → {ru2['mfe_med']-rd2['mfe_med']:+.4f}pp  "
          f"死单率差 {ru['dead']-rd['dead']:+.0f}pp → {ru2['dead']-rd2['dead']:+.0f}pp")
    print(f"     {'符号未翻转 ⇒ 非单票伪影' if (ru2['mfe_med']-rd2['mfe_med']) * (ru['mfe_med']-rd['mfe_med']) > 0 else '🔴 符号翻转 ⇒ 全样本读数是尾部伪影'}")

print("\n" + "=" * 104)
print("K2  分档单调性 (按 adverse 五档, 桶界用分位数 ⇒ 声明: 桶界属可窥探参数, 见 T41)")
print("=" * 104)
qs = sorted(adv)
cuts = [qs[int(len(qs) * p)] for p in (0.2, 0.4, 0.6, 0.8)]
labels = ["Q1(最便宜)", "Q2", "Q3", "Q4", "Q5(最贵)"]
buck = collections.defaultdict(list)
for s in S:
    i = 0
    while i < 4 and s["adverse"] > cuts[i]: i += 1
    buck[i].append(s)
for i in range(5):
    g = buck[i]
    if not g: continue
    mf = sorted(s["mfe"] for s in g)
    print(f"  {labels[i]:12s} adverse∈[{cuts[i-1] if i else min(adv):+.3f}, {cuts[i] if i < 4 else max(adv):+.3f}] "
          f"n={len(g):3d} mfe中位={st.median(mf):.4f}% 均值={st.mean(mf):+.4f}% "
          f"死单率={100*sum(1 for x in mf if x<0.3)/len(mf):.0f}% 有肉率={100*sum(1 for x in mf if x>=1.5)/len(mf):.0f}%")
med5 = [st.median(sorted(s["mfe"] for s in buck[i])) for i in range(5) if buck[i]]
mono = all(med5[j] >= med5[j+1] for j in range(len(med5)-1))
print(f"  mfe中位序列: {[round(x,4) for x in med5]}  单调递减={'是' if mono else '否 (非单调 ⇒ 轴不是线性驱动)'}")
# 点级 Spearman
def spear(xs, ys):
    n = len(xs)
    if n < 3: return None
    rx = {v: i for i, v in enumerate(sorted(xs))}; ry = {v: i for i, v in enumerate(sorted(ys))}
    dx = [rx[v] - (n-1)/2 for v in xs]; dy = [ry[v] - (n-1)/2 for v in ys]
    num = sum(a*b for a, b in zip(dx, dy))
    den = math.sqrt(sum(a*a for a in dx) * sum(b*b for b in dy))
    return num/den if den else None
sp = spear([s["adverse"] for s in S], [s["mfe"] for s in S])
print(f"  点级 Spearman(adverse, mfe) = {round(sp,4) if sp else 'NA'}  (桶级/点级并报, T41)")

print("\n" + "=" * 104)
print("K3  同日截面市场调整 (T8): 以同日全部成交笔 mfe 均值为基准算超额")
print("=" * 104)
by_day = collections.defaultdict(list)
for s in S: by_day[s["td"]].append(s["mfe"])
ex = []
for s in S:
    base = st.mean(by_day[s["td"]])
    ex.append((s["td"], s["mfe"] - base, s["adverse"]))
sp_ex = spear([x[2] for x in ex], [x[1] for x in ex])
print(f"  超额 mfe 的点级 Spearman(adverse, mfe−同日均值) = {round(sp_ex,4) if sp_ex else 'NA'}")
for nm, pred in (("追价组", lambda a: a > 0.05), ("便宜组", lambda a: a < -0.05)):
    v = [x[1] for x in ex if pred(x[2])]
    if v:
        byd = collections.defaultdict(list)
        for d, e, a in ex:
            if pred(a): byd[d].append(e)
        ds = [st.mean(x) for x in byd.values()]
        sd = st.stdev(ds) if len(ds) > 1 else 0
        t = st.mean(ds)/(sd/math.sqrt(len(ds))) if sd > 1e-12 else None
        print(f"  {nm}: n={len(v)} 超额mfe 均值={st.mean(v):+.4f}pp 中位={st.median(v):+.4f}pp 日聚类t={round(t,2) if t else 'NA'} (日数{len(byd)})")

print("\n" + "=" * 104)
print("K4  IS/OOS 对半 (按时间, 不重叠) + 双窗同号")
print("=" * 104)
Ss = sorted(S, key=lambda s: (s["td"], s["id"]))
h = len(Ss)//2
for nm, g in (("IS(前半)", Ss[:h]), ("OOS(后半)", Ss[h:])):
    u = [s for s in g if s["adverse"] > 0.05]; d = [s for s in g if s["adverse"] < -0.05]
    if u and d:
        print(f"  {nm}: n={len(g)} 窗={g[0]['td']}..{g[-1]['td']} | 追价 n={len(u)} mfe中位={st.median(sorted(s['mfe'] for s in u)):.4f}% "
              f"死单率={100*sum(1 for s in u if s['mfe']<0.3)/len(u):.0f}% | 便宜 n={len(d)} "
              f"mfe中位={st.median(sorted(s['mfe'] for s in d)):.4f}% 死单率={100*sum(1 for s in d if s['mfe']<0.3)/len(d):.0f}%")
        print(f"        mfe差(追价−便宜)={st.median(sorted(s['mfe'] for s in u))-st.median(sorted(s['mfe'] for s in d)):+.4f}pp  "
              f"死单率差={100*sum(1 for s in u if s['mfe']<0.3)/len(u) - 100*sum(1 for s in d if s['mfe']<0.3)/len(d):+.0f}pp")

print("\n" + "=" * 104)
print("K5  混淆变量: adverse 是否只是 ATR/波动/置信度的代理 (共线检验)")
print("=" * 104)
for nm, key in (("atr_pct", "atr"), ("confidence", "conf")):
    p = [(s["adverse"], s[key]) for s in S if s[key] is not None]
    if len(p) > 5:
        print(f"  Spearman(adverse, {nm}) = {round(spear([x[0] for x in p],[x[1] for x in p]),4)}  n={len(p)}")
vc = collections.defaultdict(list)
for s in S: vc[str(s["vb"])].append(s["adverse"])
for k, v in sorted(vc.items()):
    if v: print(f"  vol_bucket={k:8s} n={len(v):3d} adverse中位={st.median(v):+.4f}%")
# ATR 分档内再看效应 (控制波动)
print("\n  --- 控制 ATR 档后再看追价效应 (T13-3 波动率归一) ---")
atrs = [s["atr"] for s in S if s["atr"]]
if atrs:
    am = st.median(atrs)
    for nm, sub in (("低ATR(<中位)", [s for s in S if s["atr"] and s["atr"] < am]),
                    ("高ATR(≥中位)", [s for s in S if s["atr"] and s["atr"] >= am])):
        u = [s for s in sub if s["adverse"] > 0.05]; d = [s for s in sub if s["adverse"] < -0.05]
        if u and d:
            print(f"  {nm}: 追价 n={len(u)} mfe中位={st.median(sorted(s['mfe'] for s in u)):.4f}% 死单率={100*sum(1 for s in u if s['mfe']<0.3)/len(u):.0f}% | "
                  f"便宜 n={len(d)} mfe中位={st.median(sorted(s['mfe'] for s in d)):.4f}% 死单率={100*sum(1 for s in d if s['mfe']<0.3)/len(d):.0f}%")

print("\n" + "=" * 104)
print("K6  可达性前置 (决定「等回踩」是否可实现, T29-4 m2m 幻象): 计划价 vs chase_cap / band")
print("=" * 104)
ncap = sum(1 for s in S if s["cap"])
viol = sum(1 for s in S if s["cap"] and not s["short"] and s["e"] > s["cap"] * 1.0001)
viol_s = sum(1 for s in S if s["cap"] and s["short"] and s["e"] < s["cap"] * 0.9999)
print(f"  有 chase_cap_price 的笔数 = {ncap}/{len(S)}")
print(f"  LONG 执行价 > cap (突破禁追线) = {viol}   SHORT 执行价 < cap = {viol_s}")
print("  ⚠️ 计划价当日是否**真被触达**需 1m/5m bar ⇒ 阶段3 才能答 (band内≠触达)")
