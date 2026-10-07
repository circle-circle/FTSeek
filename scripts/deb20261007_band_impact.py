#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""⑤ band SSOT 漂移的影响面: chase_cap 归一走 fallback② 时, 畸形 band 反推出的锚错多少.
   纪律: 只测量, 不改行为. 输出 = 影响笔数 + 失真幅度分布 + 可复算命令."""
from pathlib import Path as _P
_ROOT = str(_P(__file__).resolve().parents[1])
import sqlite3, json, collections, statistics as st
c = sqlite3.connect(f"file:{_ROOT}/data/ftseek_us.db?mode=ro", uri=True); c.row_factory = sqlite3.Row
CHASE, KNIFE = 0.05, 0.03   # entry_band.py 规格常量

print("=" * 84)
print("⑤ band SSOT 漂移影响面 (chase_cap 归一锚优先级② = band 沿反推生成价)")
print("=" * 84)
rows = c.execute("""SELECT id, date, symbol, direction d, entry_price e, entry_low lo, entry_high hi,
                           raw_instruction_json raw, triggered t, executed x, skip_reason sr, atr_pct a,
                           vol_bucket vb, metadata_json mj
                    FROM playbook_instructions
                    WHERE entry_low>0 AND entry_high>0 AND entry_price>0 ORDER BY id""").fetchall()
print(f"  有 band 样本 n={len(rows)}")

# A. band 几何分类 (对照 entry_band.py 规格)
cls = collections.Counter()
distort = []      # (反推锚, 真实entry_price, 偏差%)
for r in rows:
    e = float(r["e"]); lo = float(r["lo"]); hi = float(r["hi"])
    d = str(r["d"] or "?").upper(); short = d.startswith("S")
    # 反推生成价 (pipeline.py:4905 的两个公式)
    anchor = lo / (1 - CHASE) if short else hi / (1 + CHASE)
    dev = (anchor - e) / e * 100 if e else 0
    kn = (hi - e) / e * 100 if short else (e - lo) / e * 100
    ch = (e - lo) / e * 100 if short else (hi - e) / e * 100
    if abs(kn - 3.0) < 0.05 and abs(ch - 5.0) < 0.05:
        k = "扁平3/5(符合规格)"
    elif kn < 3.0 - 0.05:
        k = "knife<3%(违反下限)"
    elif ch < 5.0 - 0.05:
        k = "chase<5%(违反只放宽)"
    elif kn > 6.05 or ch > 8.05:
        k = "超上限(knife>6/chase>8)"
    else:
        k = "vol_widen区间内(合规放宽)"
    cls[k] += 1
    if abs(dev) > 0.05:
        distort.append((r, anchor, dev, k))

tot = len(rows)
print(f"\n  --- band 几何分类 (规格: knife=max(3%,min(6%,0.9*ATR)) chase=max(5%,min(8%,1.5*ATR))) ---")
for k, v in cls.most_common():
    print(f"    {k:30s} {v:5d} ({100*v/tot:5.1f}%)")
print(f"\n  ⇒ 反推锚失真(|偏差|>0.05%)的笔数 = {len(distort)} / {tot} ({100*len(distort)/tot:.1f}%)")

# B. 失真幅度分布
if distort:
    dv = sorted(abs(x[2]) for x in distort)
    k = len(dv)
    print(f"  失真幅度: 中位={st.median(dv):.3f}% p75={dv[3*k//4]:.3f}% p90={dv[int(k*.9)]:.3f}% max={dv[-1]:.3f}%")
    byc = collections.Counter(x[3] for x in distort)
    print(f"  失真来源分类:")
    for kk, vv in byc.most_common():
        print(f"    {kk:30s} {vv:5d}")

# C. 真正受影响的子集: 有 chase_cap 且倒挂(需归一) 且 走 fallback② 的
print(f"\n  --- C. 归一实际影响面 (需 cap 倒挂 + 无 planned_entry 才走 fallback②) ---")
need_norm = 0; walk_fb2 = 0; fb2_distort = []
for r in rows:
    try:
        raw = json.loads(r["raw"]) if isinstance(r["raw"], str) and r["raw"] else {}
    except Exception:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    cap = None
    for key in ("chase_cap_price", "chase_cap"):
        v = raw.get(key)
        if v is None and isinstance(raw.get("planned_entry"), dict):
            v = raw["planned_entry"].get(key)
        if v not in (None, "", "null"):
            try: cap = float(v)
            except Exception: cap = None
            break
    e = float(r["e"]); d = str(r["d"] or "?").upper(); short = d.startswith("S")
    if cap is None or cap <= 0 or e <= 0:
        continue
    inverted = (cap >= e) if short else (cap <= e)   # 倒挂判定 (RFC-120 语义)
    if not inverted:
        continue
    need_norm += 1
    planned = raw.get("planned_entry")
    has_planned = isinstance(planned, (int, float)) and float(planned) > 0
    if not has_planned:
        walk_fb2 += 1
        anchor = float(r["lo"]) / (1 - CHASE) if short else float(r["hi"]) / (1 + CHASE)
        dev = (anchor - e) / e * 100
        if abs(dev) > 0.05:
            fb2_distort.append((r, cap, anchor, dev))
print(f"    cap 倒挂需归一的指令 n={need_norm}")
print(f"    其中走 fallback② (无 raw.planned_entry) n={walk_fb2}")
print(f"    fallback② 且 band 畸形 ⇒ 归一锚失真 n={len(fb2_distort)}")
for r, cap, anchor, dev in fb2_distort[:10]:
    print(f"      {r['symbol']:9s} {r['date']} {str(r['d'])[:9]:10s} entry={r['e']:>9} cap={cap:>9} "
          f"band=[{r['lo']:.2f},{r['hi']:.2f}] 反推锚={anchor:.2f} 偏差={dev:+.2f}%")

# D. 这些畸形 band 是谁写的 (atr_pct/vol_bucket 分布)
print(f"\n  --- D. 畸形 band 与 atr_pct 的关系 (判是否 vol_widen 未生效/ATR 缺失回落) ---")
bad = [r for r in rows if r in [x[0] for x in distort[:99999]]]
for nm, sel in (("畸形组", [x[0] for x in distort]), ("合规组", [r for r in rows if r not in [x[0] for x in distort]])):
    a = [float(r["a"]) for r in sel if r["a"] is not None]
    vb = collections.Counter(str(r["vb"] or "NULL") for r in sel)
    dt = sorted(set(str(r["date"]) for r in sel))
    print(f"    {nm:8s} n={len(sel):4d}  atr_pct 中位={st.median(a)*100 if a else 0:5.2f}% (n={len(a)})  "
          f"日期范围={dt[0] if dt else '?'}~{dt[-1] if dt else '?'}")
    print(f"             {'':8s}  vol_bucket={dict(vb.most_common(5))}")

print()
print("=" * 84)
print("⑥ 复算命令 (供第二 agent 独立验证)")
print("=" * 84)
print("  口径: 规格常量 CHASE=0.05 KNIFE=0.03 取自 app/playbook/entry_band.py:24-25")
print("  反推公式取自 app/playbook/pipeline.py:4905 注释 (entry_low/(1-CHASE) | entry_high/(1+CHASE))")
