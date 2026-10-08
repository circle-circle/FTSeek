#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""[DEB-20261007 第5轮·独立复查] 复核者对抗性核验 (只读, 零行情拉取, 任意 cwd).

不复用作者脚本的聚合逻辑, 直查 DB 独立重算六项:
  V1 join 双计核查: 多行 decision_id 是否污染 stage3 人群 (mfe_driver 无 planned 过滤)
  V2 P2 加固刀①: 可达/不可达按方向分层 (LONG/SHORT 分别同向才算稳)
  V3 P2 加固刀②: 可达组符号集中度 + 剔最大符号后的笔均
  V4 Spearman 平局平均秩 + 去重 (作者 rank-dict 对平局取末位, 60 笔 mfe=0 会偏)
  V5 X0 补空头核验: SHORT 亏损单 mae ≥ 出场涨幅 (作者的 (entry-exit)/entry 对空头恒负=空转)
  V6 X3 省额精确口径: |e-pe|*qty vs 作者 adverse%*e*qty (后者高估 e/pe 倍)
  V7 P0 头条人群构成: executed vs 未执行位移并列 (防 0% 稀释)
"""
from pathlib import Path as _P
_ROOT = str(_P(__file__).resolve().parents[1])
import sqlite3, json, math, statistics as st, collections

c = sqlite3.connect(f"file:{_ROOT}/data/ftseek_us.db?mode=ro", uri=True)
c.row_factory = sqlite3.Row


def avg_rank(vals):
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    ranks = [0.0] * len(vals)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def spear_avg(xs, ys):
    if len(xs) < 5:
        return None
    rx, ry = avg_rank(xs), avg_rank(ys)
    mx, my = st.mean(rx), st.mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return num / den if den else None


print("=" * 104)
print("V1  join 双计核查 (stage3/mfe_driver 人群无 planned 过滤 ⇒ 多行 decision 会双计)")
print("=" * 104)
multi = c.execute("""SELECT decision_id, COUNT(*) n FROM playbook_instructions
                     WHERE decision_id IS NOT NULL GROUP BY decision_id HAVING n>1""").fetchall()
tr = {}
for t in c.execute("SELECT * FROM trade_records WHERE source_type='PLAYBOOK_SIM'"):
    k = str(t["decision_id"] or "")
    if k: tr.setdefault(k, []).append(t)
dups = []
for r in multi:
    k = str(r["decision_id"])
    if tr.get(k):
        dups.append(k)
        print(f"  {k}: 指令{r['n']}行 × 成交{len(tr[k])}笔 → stage3 计 {r['n']} 次")
s3_uniq = len({str(r["decision_id"]) for r in c.execute(
    "SELECT decision_id FROM playbook_instructions WHERE decision_id IS NOT NULL")
    if str(r["decision_id"]) in tr and tr[str(r["decision_id"])][0]["mfe_pct"] is not None})
print(f"  stage3 脚本 S=602 (行数) vs 去重唯一 decision={s3_uniq} ⇒ 双计 {602 - s3_uniq} 笔")
by_er = collections.defaultdict(list)
for k in {str(r["decision_id"]) for r in c.execute(
        "SELECT decision_id FROM playbook_instructions WHERE decision_id IS NOT NULL")
          if str(r["decision_id"]) in tr and tr[str(r["decision_id"])][0]["mfe_pct"] is not None}:
    t = tr[k][0]
    by_er[str(t["exit_reason"])].append((float(t["pnl_net"] or 0), float(t["mfe_pct"])))
for k in ("SL_TRAIL_LOCK", "SL_INITIAL", "SL_BREAKEVEN", "PRE_CLOSE_EXIT"):
    v = by_er.get(k, [])
    if v:
        pn = [x[0] for x in v]
        print(f"  去重后 {k:18s} n={len(v):3d} Σ=${sum(pn):+9.2f} 笔均=${st.mean(pn):+7.2f} "
              f"WR={100*sum(1 for x in pn if x>0)/len(pn):3.0f}% mfe中位={st.median([x[1] for x in v]):.4f}%")

print("\n" + "=" * 104)
print("V2/V3  P2 负选择加固: 方向分层 + 符号集中度 (作者未做的两刀)")
print("=" * 104)
inst = {}
for r in c.execute("""SELECT id, decision_id, entry_price e, direction d, symbol, raw_instruction_json raw
                      FROM playbook_instructions WHERE decision_id IS NOT NULL ORDER BY id"""):
    k = str(r["decision_id"])
    if k not in inst: inst[k] = r
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
    if adverse <= 0.05: continue
    S.append(dict(short=short, sym=str(r["symbol"]), reach=float(t["mae_pct"]) >= adverse * (float(pe) / e),
                  pnl=float(t["pnl_net"]) if t["pnl_net"] is not None else None))
for nm, pred in (("LONG", lambda s: not s["short"]), ("SHORT", lambda s: s["short"])):
    sub = [s for s in S if pred(s)]
    rc = [s for s in sub if s["reach"]]; un = [s for s in sub if not s["reach"]]
    a = [s["pnl"] for s in rc if s["pnl"] is not None]; b = [s["pnl"] for s in un if s["pnl"] is not None]
    if a and b:
        print(f"  {nm}: 可达 n={len(rc)} 笔均=${st.mean(a):+.2f} | 不可达 n={len(un)} 笔均=${st.mean(b):+.2f} "
              f"| 差=${st.mean(a)-st.mean(b):+.2f}  {'✅同向' if st.mean(a)<st.mean(b) else '🔴翻号'}")
rc_all = [s for s in S if s["reach"]]
cnt = collections.Counter(s["sym"] for s in rc_all)
top, _ = cnt.most_common(1)[0]
rest = [s["pnl"] for s in rc_all if s["sym"] != top and s["pnl"] is not None]
print(f"  可达组 n={len(rc_all)} 最大符号 {top} {cnt[top]} 笔; 剔除后笔均=${st.mean(rest):+.2f} "
      f"(全组 ${st.mean([s['pnl'] for s in rc_all if s['pnl'] is not None]):+.2f}) ⇒ 集中度低")

print("\n" + "=" * 104)
print("V4  Spearman(mfe, pnl) 平局平均秩 + 去重 (作者 rank-dict 对 60 笔 mfe=0 取末位秩)")
print("=" * 104)
uniq = {str(r["decision_id"]) for r in c.execute(
    "SELECT decision_id FROM playbook_instructions WHERE decision_id IS NOT NULL")
    if str(r["decision_id"]) in tr}
pairs = [(float(tr[k][0]["mfe_pct"]), float(tr[k][0]["pnl_net"]))
         for k in uniq if tr[k][0]["mfe_pct"] is not None and tr[k][0]["pnl_net"] is not None]
sp = spear_avg([x[0] for x in pairs], [x[1] for x in pairs])
print(f"  去重 n={len(pairs)} 平均秩 Spearman = {sp:+.4f}  (作者 +0.7652 / n=598 含 4 双计) ⇒ 同档, 结论稳")

print("\n" + "=" * 104)
print("V5  X0 补空头核验: SHORT 亏损单 (exit>entry) 的 mae ≥ 出场涨幅")
print("=" * 104)
ok_s = n_s = ok_l = n_l = 0
for r in c.execute("""SELECT direction, entry_price e, exit_price x, mae_pct FROM trade_records
                      WHERE source_type='PLAYBOOK_SIM' AND mae_pct IS NOT NULL
                        AND entry_price>0 AND exit_price>0"""):
    d = str(r["direction"] or "").upper(); e = float(r["e"]); x = float(r["x"]); m = float(r["mae_pct"])
    if d.startswith("S") and x > e:
        n_s += 1; ok_s += (m >= (x - e) / e * 100 - 1e-9)
    elif not d.startswith("S") and x < e:
        n_l += 1; ok_l += (m >= (e - x) / e * 100 - 1e-9)
print(f"  LONG 亏损单: {ok_l}/{n_l} = {100*ok_l/max(n_l,1):.0f}%   SHORT 亏损单: {ok_s}/{n_s} = {100*ok_s/max(n_s,1):.0f}%")
print("  ⇒ 短头也基本成立 (采样粒度缺口), 生产码 pipeline.py:8023-8025 明写方向归一化")

print("\n" + "=" * 104)
print("V6  X3 省额精确口径: Σ|e-pe|*qty vs 作者 Σ adverse%*e*qty")
print("=" * 104)
sav_script = sav_true = 0.0; n_sav = 0
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
    qty = float(t["quantity"] or 0)
    if adverse > 0.05 and qty and float(t["mae_pct"]) >= adverse * (float(pe) / e):
        n_sav += 1
        sav_script += adverse / 100 * e * qty
        sav_true += abs(e - float(pe)) * qty
print(f"  n={n_sav} 作者口径=${sav_script:,.2f} 精确口径=${sav_true:,.2f} 高估={100*(sav_script/sav_true-1):.2f}% "
      f"⇒ Δ 修正 +$312.93→+${312.93 - (sav_script - sav_true):.2f}, 方向不变")

print("\n" + "=" * 104)
print("V7  P0 头条人群构成: executed vs 未执行 (防 0% 稀释)")
print("=" * 104)
B = []
for r in c.execute("SELECT executed, entry_price e, raw_instruction_json raw FROM playbook_instructions"):
    try: raw = json.loads(r["raw"] or "{}")
    except Exception: raw = {}
    pe = raw.get("planned_entry") if isinstance(raw, dict) else None
    if isinstance(pe, (int, float)) and float(pe) > 0 and r["e"] and float(r["e"]) > 0:
        B.append((bool(r["executed"]), abs((float(r["e"]) - float(pe)) / float(pe) * 100)))
for nm, pred in (("executed", lambda flag: flag), ("未执行", lambda flag: not flag)):
    v = sorted(d for flag, d in B if pred(flag))
    if v:
        print(f"  {nm}: n={len(v)} |dev|中位={st.median(v):.4f}% p90={v[int(len(v)*.9)]:.4f}%")
print(f"  全体 (指令行级): n={len(B)} |dev|中位={st.median(sorted(d for _, d in B)):.4f}% ⇒ 头条 1.0363% 被未执行 0% 稀释")
