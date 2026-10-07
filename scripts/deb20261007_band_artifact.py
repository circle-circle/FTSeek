#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""⑰ 自曝验证: 我报的「45.2% band 违反只放宽规格」是否是用被污染的 entry_price 当锚算出的伪影?
   若用 raw.planned_entry (真生成锚) 重算 band 几何, 违反率应趋零 ⇒ 伪影;
   若仍违反 ⇒ 真漂移."""
from pathlib import Path as _P
_ROOT = str(_P(__file__).resolve().parents[1])
import sqlite3, json, collections, statistics as st
c = sqlite3.connect(f"file:{_ROOT}/data/ftseek_us.db?mode=ro", uri=True); c.row_factory = sqlite3.Row
CHASE, KNIFE = 0.05, 0.03

rows = c.execute("""SELECT id, date, symbol, direction d, entry_price e, entry_low lo, entry_high hi,
                           raw_instruction_json raw, atr_pct a, vol_bucket vb
                    FROM playbook_instructions
                    WHERE entry_low>0 AND entry_high>0 AND entry_price>0 ORDER BY id""").fetchall()

def classify(anchor, lo, hi, short):
    """按 anchor 算 knife/chase 并分类."""
    if anchor <= 0: return None, None, "无锚"
    kn = (hi - anchor) / anchor * 100 if short else (anchor - lo) / anchor * 100
    ch = (anchor - lo) / anchor * 100 if short else (hi - anchor) / anchor * 100
    if abs(kn - 3.0) < 0.05 and abs(ch - 5.0) < 0.05: return kn, ch, "扁平3/5(合规)"
    if kn < 3.0 - 0.05: return kn, ch, "knife<3%(违反下限)"
    if ch < 5.0 - 0.05: return kn, ch, "chase<5%(违反只放宽)"
    if kn > 6.05 or ch > 8.05: return kn, ch, "超上限"
    return kn, ch, "vol_widen区间内(合规放宽)"

cls_db = collections.Counter(); cls_pe = collections.Counter()
mismatch = []
n_pe = 0
for r in rows:
    e = float(r["e"]); lo = float(r["lo"]); hi = float(r["hi"])
    d = str(r["d"] or "?").upper(); short = d.startswith("S")
    try:
        raw = json.loads(r["raw"]) if isinstance(r["raw"], str) and r["raw"] else {}
    except Exception:
        raw = {}
    pe = raw.get("planned_entry") if isinstance(raw, dict) else None
    pe = float(pe) if isinstance(pe, (int, float)) and float(pe) > 0 else None

    _, _, k1 = classify(e, lo, hi, short)
    cls_db[k1] += 1
    if pe:
        n_pe += 1
        kn2, ch2, k2 = classify(pe, lo, hi, short)
        cls_pe[k2] += 1
        if k1 != k2:
            mismatch.append((r, e, pe, kn2, ch2, k1, k2))

tot = len(rows)
print(f"样本 n={tot}   其中可取 planned_entry n={n_pe}")
print()
print("=" * 92)
print("band 几何分类: 用 DB entry_price 当锚 (我上一版的口径) vs 用 planned_entry 当真锚")
print("=" * 92)
print(f"  {'分类':32s} {'entry_price锚':>14s} {'planned_entry锚':>16s}")
for k in ("扁平3/5(合规)", "vol_widen区间内(合规放宽)", "chase<5%(违反只放宽)", "knife<3%(违反下限)", "超上限"):
    a = cls_db.get(k, 0); b = cls_pe.get(k, 0)
    print(f"  {k:32s} {a:6d} ({100*a/tot:5.1f}%) {b:8d} ({100*b/max(n_pe,1):5.1f}%)")

viol_db = cls_db.get("chase<5%(违反只放宽)", 0) + cls_db.get("knife<3%(违反下限)", 0)
viol_pe = cls_pe.get("chase<5%(违反只放宽)", 0) + cls_pe.get("knife<3%(违反下限)", 0)
print()
print(f"  ⇒ 违反「只放宽不收窄」规格: entry_price锚 = {viol_db}/{tot} ({100*viol_db/tot:.1f}%)")
print(f"                              planned_entry锚 = {viol_pe}/{n_pe} ({100*viol_pe/max(n_pe,1):.1f}%)")
if viol_pe < viol_db * 0.2:
    print(f"\n  🔴 自曝成立: 我上一版报的「45.2% band 违反规格」是**用被 reprice 污染的 entry_price 当锚算出的伪影**")
    print(f"     用真生成锚 planned_entry 重算, 违反率 {100*viol_db/tot:.1f}% → {100*viol_pe/max(n_pe,1):.1f}%")
    print(f"     ⇒ band 几何本身合规; 真问题是 RFC-123 描述的 entry_price 生命周期混用")
elif viol_pe > viol_db * 0.5:
    print(f"\n  ⚠ 违反率未大幅下降 ⇒ band 几何确有不合规, 需查写入侧")
else:
    print(f"\n  部分下降: 两种口径都有贡献, 需分别量化")

print()
print("=" * 92)
print("分类翻转明细 (前 12 笔): 同一 band 在两个锚下的判定差异")
print("=" * 92)
for r, e, pe, kn2, ch2, k1, k2 in mismatch[:12]:
    print(f"  {r['symbol']:9s} {r['date']} {str(r['d'])[:9]:10s} entry_price={e:>9.2f} planned={pe:>9.2f} "
          f"band=[{float(r['lo']):.2f},{float(r['hi']):.2f}] atr={r['a']}")
    print(f"      {'':9s} entry_price锚判定={k1:28s} planned锚判定={k2} (knife={kn2:.2f}% chase={ch2:.2f}%)")
print(f"  分类翻转总数 = {len(mismatch)} / {n_pe} ({100*len(mismatch)/max(n_pe,1):.1f}%)")

print()
print("=" * 92)
print("补: planned_entry 锚下的 knife/chase 实际值 vs ATR (验 vol_widen 是否按规格生效)")
print("=" * 92)
recs = []
for r in rows:
    lo = float(r["lo"]); hi = float(r["hi"])
    d = str(r["d"] or "?").upper(); short = d.startswith("S")
    try:
        raw = json.loads(r["raw"]) if isinstance(r["raw"], str) and r["raw"] else {}
    except Exception:
        raw = {}
    pe = raw.get("planned_entry") if isinstance(raw, dict) else None
    pe = float(pe) if isinstance(pe, (int, float)) and float(pe) > 0 else None
    if not pe: continue
    kn, ch, _ = classify(pe, lo, hi, short)
    if kn is None: continue
    a = float(r["a"]) if r["a"] else None
    recs.append((kn, ch, a, str(r["vb"] or "NULL")))
print(f"  n={len(recs)}")
byv = collections.defaultdict(list)
for kn, ch, a, vb in recs: byv[vb].append((kn, ch, a))
for vb, v in sorted(byv.items(), key=lambda kv: -len(kv[1])):
    kns = sorted(x[0] for x in v); chs = sorted(x[1] for x in v)
    ats = [x[2] for x in v if x[2]]
    k = len(v)
    exp_kn = f"max(3,min(6,0.9*ATR))" if ats else "?"
    print(f"  {vb:10s} n={k:4d} knife 中位={st.median(kns):5.2f}% p25={kns[k//4]:5.2f}% p75={kns[3*k//4]:5.2f}% | "
          f"chase 中位={st.median(chs):5.2f}% p25={chs[k//4]:5.2f}% p75={chs[3*k//4]:5.2f}% | ATR中位={st.median(ats)*100 if ats else 0:.2f}%")
print(f"\n  ⇒ vol_widen 规格: knife=max(3%,min(6%,0.9*ATR%)) chase=max(5%,min(8%,1.5*ATR%))")
print(f"     生产开关 entry_band_vol_widen_enabled=false ⇒ 期望全部落在扁平 3.00/5.00")
