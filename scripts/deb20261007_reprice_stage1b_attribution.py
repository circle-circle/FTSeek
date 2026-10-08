#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""[DEB-20261007 追问 · 阶段1b] 口径差异归因 + 尾部诊断 + 时刻语义.

三个必须钉死的问题:
  Q1  reprice_audit(中位|x|0.44%, n=156) vs E18 band反推(中位1.01%, n=605) 为何差 2 倍?
      ⇒ 查两人群是否同一批笔、reprice 是否多次发生、audit 是否只留最后一次
  Q2  up组 pnl_net 均值 −8.15 但中位 +10.11 ⇒ 均值被尾部主导 (T18)?
      ⇒ 剔尾/尾部贡献/按日聚类三诊断
  Q3  reprice 的时刻语义: 是"价格已动追上去"还是"到点了价格偏离"?
      ⇒ reprice_audit.at vs created_at vs execution_time 的间隔分布
      (决定策略含义: 该不该追 vs 挂限价等回踩能否成交)
"""
from pathlib import Path as _P
_ROOT = str(_P(__file__).resolve().parents[1])
import sqlite3, json, math, statistics as st, collections
from datetime import datetime

c = sqlite3.connect(f"file:{_ROOT}/data/ftseek_us.db?mode=ro", uri=True)
c.row_factory = sqlite3.Row


def _dt(s):
    s = str(s or "").strip()
    if not s:
        return None
    _fmt_misses = 0
    for f in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S.%f",
              "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(s.replace("Z", "+0000"), f)
        except Exception:
            _fmt_misses += 1  # 2026-10-08 棘轮清剿: 格式 miss 计数 (逐档试是设计, 不逐档刷屏)
            continue
    try:
        return datetime.fromisoformat(s)
    except Exception:
        # 全部格式 + fromisoformat 均失败 = 真坏串, 必须可见 (原为静默 return None 前的吞错)
        print(f"[warn] 时间串无法解析 ({_fmt_misses} 档格式均 miss): {s!r}")
        return None


rows = c.execute("""SELECT id, date, symbol, direction d, entry_price e, entry_low lo, entry_high hi,
                           execution_price xp, executed x, triggered t, skip_reason sr,
                           raw_instruction_json raw, metadata_json md, anchor_dev_pct ad, atr_pct atr,
                           decision_id did, execution_time xt, created_at ca
                    FROM playbook_instructions ORDER BY id""").fetchall()

A, B = [], []
for r in rows:
    try: md = json.loads(r["md"]) if r["md"] else {}
    except Exception: md = {}
    if not isinstance(md, dict): md = {}
    ra = md.get("reprice_audit") or {}
    rec = dict(r=r, id=r["id"], did=str(r["did"] or ""), date=r["date"])
    if isinstance(ra, dict) and ra.get("reprice_from") and ra.get("reprice_to"):
        try: f_, t_ = float(ra["reprice_from"]), float(ra["reprice_to"])
        except Exception: f_ = t_ = 0
        if f_ > 0 and t_ > 0:
            rec.update(f=f_, t=t_, dev=(t_-f_)/f_*100, at=ra.get("at"), ra=ra)
            A.append(dict(rec))
    try: raw = json.loads(r["raw"]) if r["raw"] else {}
    except Exception: raw = {}
    if isinstance(raw, dict) and isinstance(raw.get("planned_entry"), (int, float)) and float(raw["planned_entry"]) > 0:
        pe = float(raw["planned_entry"])
        if r["e"] and float(r["e"]) > 0:
            rec.update(pe=pe, dev_b=(float(r["e"])-pe)/pe*100)
            B.append(dict(rec))

idsA = {a["id"] for a in A}; idsB = {b["id"] for b in B}

print("=" * 96)
print("Q1  口径差异归因: 为什么 reprice_audit 中位 0.44% 而 E18 band反推中位 1.01%?")
print("=" * 96)
print(f"  A(reprice_audit) n={len(A)}  B(planned_entry) n={len(B)}  A∩B={len(idsA & idsB)}  A−B={len(idsA-idsB)}  B−A={len(idsB-idsA)}")
both = {a["id"]: a for a in A if a["id"] in idsB}
bmap = {b["id"]: b for b in B}
print("\n  --- 同一批笔 (A∩B) 上, 两个口径逐笔对比 ---")
same, diff, samples = 0, 0, []
for i, a in both.items():
    b = bmap[i]
    dA, dB = abs(a["dev"]), abs(b["dev_b"])
    if abs(dA - dB) < 0.05: same += 1
    else:
        diff += 1
        samples.append((i, a, b))
print(f"  两口径一致(<0.05pp) = {same}/{len(both)}   不一致 = {diff}/{len(both)}")
if samples:
    print("  不一致样本 (前8): reprice_audit dev vs (entry−planned) dev")
    for i, a, b in samples[:8]:
        print(f"    id={i} {a['date']} {a['r']['symbol']:9s} from={a['f']:>9.2f} to={a['t']:>9.2f} "
              f"devA={a['dev']:+.3f}% | planned={b['pe']:>9.2f} entry={a['r']['e']:>9.2f} devB={b['dev_b']:+.3f}%")
    dA = sorted(abs(a["dev"]) for _, a, _ in samples); dB = sorted(abs(b["dev_b"]) for _, _, b in samples)
    print(f"    不一致子集: |devA|中位={st.median(dA):.4f}%  |devB|中位={st.median(dB):.4f}%")

print("\n  --- B−A (有 planned_entry 但无 reprice_audit) 的位移分布 ---")
onlyB = [b for b in B if b["id"] not in idsA]
if onlyB:
    v = sorted(abs(b["dev_b"]) for b in onlyB); n = len(v)
    print(f"  n={n} |devB|中位={st.median(v):.4f}% p90={v[int(n*.9)]:.4f}% max={v[-1]:.4f}%")
    print(f"  ⇒ 若此子集位移显著更大, 说明 reprice_audit **只覆盖了小幅 reprice 的子集** (选择偏差)")
    vA = sorted(abs(a["dev"]) for a in A)
    print(f"  对照 A 全体 |dev|中位={st.median(vA):.4f}%")
    z = sum(1 for x in v if x > st.median(vA))
    print(f"  B−A 子集中超过 A 中位的占比 = {100*z/n:.1f}%")

print("\n  --- A 人群里 reprice_to 与 DB entry_price / execution_price 的关系 ---")
cnt = collections.Counter()
for a in A:
    e = float(a["r"]["e"] or 0); x = float(a["r"]["xp"] or 0)
    if e and abs(a["t"]-e)/e < 0.001: cnt["entry_price==reprice_to"] += 1
    elif e: cnt["entry_price≠reprice_to"] += 1
    if x and abs(a["t"]-x)/x < 0.001: cnt["exec_price==reprice_to"] += 1
print(f"  {dict(cnt)}")
print("  ⇒ 若 entry_price≠reprice_to 占比高, 说明 reprice 发生过**多次**, audit 只留一次 ⇒ A 低估全程位移")

print("\n" + "=" * 96)
print("Q2  up组(追价买贵) pnl_net 均值−8.15 vs 中位+10.11 ⇒ 尾部诊断 (T18)")
print("=" * 96)
tr = {}
for t in c.execute("SELECT * FROM trade_records WHERE source_type='PLAYBOOK_SIM'"):
    k = str(t["decision_id"] or "")
    if k: tr.setdefault(k, []).append(t)

for label, pred in (("down(买得更便宜)", lambda d: d < -0.05), ("up(追价买贵)", lambda d: d > 0.05)):
    g = []
    for a in A:
        if a["did"] in tr and pred(a["dev"]):
            for t in tr[a["did"]]: g.append((a, t))
    if not g:
        print(f"  {label}: n=0"); continue
    pn = sorted(((float(t["pnl_net"]), a["r"]["symbol"], t["trade_date"]) for _, t in g if t["pnl_net"] is not None),
                key=lambda x: x[0])
    tot = sum(x[0] for x in pn); n = len(pn)
    print(f"\n  {label}: n={n} 合计=${tot:.2f} 均值=${tot/n:+.2f} 中位=${st.median([x[0] for x in pn]):+.2f}")
    k = min(3, n)
    worst = sum(x[0] for x in pn[:k])
    print(f"    最亏{k}笔合计=${worst:.2f} 占总额比={100*worst/tot if tot else float('nan'):.0f}% "
          f"⇒ 剔除后合计=${tot-worst:.2f} 均值=${(tot-worst)/(n-k):+.2f}")
    print(f"    最亏{k}笔: {[(f'{v:.0f}', s, d) for v, s, d in pn[:k]]}")
    wr = 100*sum(1 for x in pn if x[0] > 0)/n
    print(f"    WR={wr:.0f}%  正单笔数={sum(1 for x in pn if x[0]>0)}/{n}")
    # mfe
    mf = [float(t["mfe_pct"]) for _, t in g if t["mfe_pct"] is not None]
    if mf:
        ms = sorted(mf)
        print(f"    mfe_pct: 中位={st.median(ms):.4f}% 均值={st.mean(ms):.4f}% p90={ms[int(len(ms)*.9)]:.4f}%")
        print(f"    mfe≥1.5%(有肉)占比={100*sum(1 for x in ms if x>=1.5)/len(ms):.0f}%  "
              f"mfe<0.3%(死单)占比={100*sum(1 for x in ms if x<0.3)/len(ms):.0f}%")

print("\n" + "=" * 96)
print("Q3  reprice 时刻语义: 是「价格已动追上去」还是「到点了价格偏离」?")
print("=" * 96)
gaps_at_xt, gaps_ca_at = [], []
for a in A:
    at = _dt(a.get("at")); xt = _dt(a["r"]["xt"]); ca = _dt(a["r"]["ca"])
    if at and xt:
        gaps_at_xt.append((xt - at).total_seconds() / 60.0)   # reprice→执行
    if at and ca:
        gaps_ca_at.append((at - ca).total_seconds() / 60.0)    # 指令生成→reprice


def _p(v, lb, unit="min"):
    if not v: print(f"  {lb}: 空"); return
    s = sorted(v); n = len(s)
    print(f"  {lb}: n={n} 中位={st.median(s):+.1f}{unit} p10={s[int(n*.1)]:+.1f} p90={s[int(n*.9)]:+.1f} "
          f"min={s[0]:+.1f} max={s[-1]:+.1f}")


_p(gaps_ca_at, "指令生成 → reprice 发生")
_p(gaps_at_xt, "reprice → 实际执行")
print("\n  判读:")
if gaps_ca_at and st.median(gaps_ca_at) > 30:
    print(f"    指令生成到 reprice 中位 {st.median(gaps_ca_at):.0f} 分钟 ⇒ reprice 是**盘中价格演化后**的重新定价")
    print("    ⇒ 策略含义是「该不该追/等回踩」, 不是「计划价写错了」")
elif gaps_ca_at:
    print(f"    指令生成到 reprice 中位 {st.median(gaps_ca_at):.0f} 分钟 ⇒ reprice 紧跟生成 ⇒ 更像定价修正")
if gaps_at_xt:
    print(f"    reprice 到执行中位 {st.median(gaps_at_xt):.0f} 分钟 ⇒ "
          f"{'执行紧跟 reprice (市价追单语义)' if abs(st.median(gaps_at_xt)) < 5 else '执行与 reprice 有间隔'}")
