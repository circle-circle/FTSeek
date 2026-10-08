#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""[DEB-20261007 第6轮] DA 盘前定价时窗 vs 位移/结局 (只读, 任意 cwd).

用户问: 「entry_price 能否通过 DA LLM 基于盘前(什么时间段)和历史数据找到合理价格?」
可判定部分量化:
  T1 DA 指令真实生成时窗 (ET 小时分布, 剔除回填批: created ET 日期 ≠ 指令 date)
  T2 生成时窗 → |planned→executed| 位移 / 追价占比 / mfe / pnl (更晚 = 更准?)
  T3 04-06 时 vs 07-08 时 (主链窗) 位移对照 + Spearman(生成小时, |dev|)
  T4 DA 内部位移轴复验: Spearman(|dev|, mfe) / (adverse, mfe) — 与全源 P1 是否同判
机制锚点 (读码, 不本脚本复算):
  DA 主链窗 07:55-08:15 ET (ops/hermes/da_main_carrier.py 的 755<=minute<=815 守卫)
  更早 deep-stock-analysis Hermes job 20:00 CST ≈ 04:00 ET
  planned_entry = DA LLM 直写 (deep-stock-analysis/scripts/pipeline.py planned_entry=vr["entry_price"])
  prompt 硬规: 入场锚定盘前有效现价, 严禁锚昨收 (llm_synth.py 盘前段)
"""
from pathlib import Path as _P
_ROOT = str(_P(__file__).resolve().parents[1])
import sqlite3, json, math, statistics as st, collections
from datetime import datetime
from zoneinfo import ZoneInfo

c = sqlite3.connect(f"file:{_ROOT}/data/ftseek_us.db?mode=ro", uri=True)
c.row_factory = sqlite3.Row
ET = ZoneInfo("America/New_York")

tr = {}
for t in c.execute("SELECT * FROM trade_records WHERE source_type='PLAYBOOK_SIM'"):
    k = str(t["decision_id"] or "")
    if k: tr.setdefault(k, []).append(t)

S = []
backfill = 0
for r in c.execute("""SELECT id, decision_id, date, created_at, entry_price e, direction d,
                             raw_instruction_json raw
                      FROM playbook_instructions WHERE decision_id LIKE 'DA-%' ORDER BY id"""):
    try: raw = json.loads(r["raw"] or "{}")
    except Exception: raw = {}
    pe = raw.get("planned_entry") if isinstance(raw, dict) else None
    if not (isinstance(pe, (int, float)) and float(pe) > 0): continue
    try: created = datetime.fromisoformat(str(r["created_at"]).replace("Z", "+00:00"))
    except Exception: continue
    created_et = created.astimezone(ET)
    if created_et.strftime("%Y-%m-%d") != str(r["date"]):
        backfill += 1
        continue
    hour = created_et.hour
    if not (4 <= hour <= 12): continue
    e = float(r["e"] or 0)
    k = str(r["decision_id"])
    if e <= 0 or k not in tr: continue
    t = tr[k][0]
    if t["mfe_pct"] is None: continue
    short = str(r["d"] or "").upper().startswith("S")
    dev = (e - float(pe)) / float(pe) * 100
    S.append(dict(hour=hour, adev=abs(dev), adverse=dev if not short else -dev,
                  mfe=float(t["mfe_pct"]),
                  pnl=float(t["pnl_net"]) if t["pnl_net"] is not None else None,
                  td=t["trade_date"]))

print(f"DA 已成交 ∧ 有 planned ∧ 当日真实生成 (非回填) n={len(S)}  剔除回填/跨日 n={backfill}")

print("\n" + "=" * 104)
print("T1  DA 生成时窗 (ET 小时) — 主链 07:55-08:15 vs 更早扫描")
print("=" * 104)
hours = collections.Counter(s["hour"] for s in S)
for h in sorted(hours):
    print(f"  {h:02d}时 ET: n={hours[h]:3d}  {'(DA 主链窗 07:55-08:15)' if h == 8 else ''}")

print("\n" + "=" * 104)
print("T2  生成时窗 → 位移与结局 (n<15 只观察; 位移更小≠更赚钱)")
print("=" * 104)
print(f"{'ET时':>4} {'n':>4} {'|dev|中位':>9} {'追价占比':>8} {'mfe中位':>8} {'笔均pnl':>10}")
by = collections.defaultdict(list)
for s in S: by[s["hour"]].append(s)
for h in sorted(by):
    g = by[h]
    if len(g) < 15:
        print(f"{h:4d} {len(g):4d}  (n<15 只观察)")
        continue
    pn = [x["pnl"] for x in g if x["pnl"] is not None]
    print(f"{h:4d} {len(g):4d} {st.median(sorted(x['adev'] for x in g)):9.3f}% "
          f"{100*sum(1 for x in g if x['adverse']>0.05)/len(g):7.0f}% "
          f"{st.median(sorted(x['mfe'] for x in g)):8.4f}% "
          f"${st.mean(pn) if pn else 0:+10.2f}")

print("\n" + "=" * 104)
print("T3  早窗 vs 主链窗对照 + 时序秩相关")
print("=" * 104)
for nm, pred in (("04-06时 (早扫描窗)", lambda h: h <= 6), ("07-08时 (DA 主链窗)", lambda h: 7 <= h <= 8)):
    g = [s for s in S if pred(s["hour"])]
    if len(g) < 15:
        print(f"  {nm}: n={len(g)} 样本不足")
        continue
    pn = [x["pnl"] for x in g if x["pnl"] is not None]
    print(f"  {nm}: n={len(g):3d} |dev|中位={st.median(sorted(x['adev'] for x in g)):.3f}% "
          f"追价占比={100*sum(1 for x in g if x['adverse']>0.05)/len(g):.0f}% "
          f"mfe中位={st.median(sorted(x['mfe'] for x in g)):.4f}% "
          f"笔均pnl=${st.mean(pn):+.2f}")


def spear_avg(xs, ys):
    if len(xs) < 8: return None

    def avg_rank(v):
        order = sorted(range(len(v)), key=lambda i: v[i]); rk = [0.0]*len(v); i = 0
        while i < len(order):
            j = i
            while j+1 < len(order) and v[order[j+1]] == v[order[i]]: j += 1
            for kk in range(i, j+1): rk[order[kk]] = (i+j)/2+1
            i = j+1
        return rk
    rx, ry = avg_rank(xs), avg_rank(ys); mx, my = st.mean(rx), st.mean(ry)
    num = sum((a-mx)*(b-my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a-mx)**2 for a in rx)*sum((b-my)**2 for b in ry))
    return num/den if den else None


sp_h = spear_avg([s["hour"] for s in S], [s["adev"] for s in S])
print(f"  Spearman(生成ET小时, |dev|) = {sp_h:+.4f}  (负=越晚生成位移越小)")

print("\n" + "=" * 104)
print("T4  DA 内部位移轴复验 (与全源 P1 六刀是否同判)")
print("=" * 104)
sp1 = spear_avg([s["adev"] for s in S], [s["mfe"] for s in S])
sp2 = spear_avg([s["adverse"] for s in S], [s["mfe"] for s in S])
pnS = [s for s in S if s["pnl"] is not None]
sp3 = spear_avg([s["adev"] for s in pnS], [s["pnl"] for s in pnS])
print(f"  Spearman(|dev|, mfe) = {sp1:+.4f}   Spearman(adverse, mfe) = {sp2:+.4f}   "
      f"Spearman(|dev|, pnl) = {sp3:+.4f}")
print("  ⇒ 若均近零: DA 子集与全源 P1 同判 — 位移轴无信息, 时窗只改位移不改结局")
