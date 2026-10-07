#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""⑩ 决定性判别: band 反推锚失真 61% 的两个互斥解释
   (a) RFC-123 生命周期混用: entry_price 被 reprice 覆盖 ⇒ band 反映生成价, entry_price 反映执行价
   (b) T4 双轨: entry_low/high 是 LLM 散文技术位, 从来不是 compute_entry_band 输出
   判据: 反推锚 vs raw_instruction_json.planned_entry (真实生成价)
         若两者吻合 ⇒ (a) 成立, band 是 SSOT 输出但被 reprice 污染
         若两者不吻合 ⇒ (b) 成立, band 是散文位"""
from pathlib import Path as _P
_ROOT = str(_P(__file__).resolve().parents[1])
import sqlite3, json, statistics as st, collections
c = sqlite3.connect(f"file:{_ROOT}/data/ftseek_us.db?mode=ro", uri=True); c.row_factory = sqlite3.Row
CHASE, KNIFE = 0.05, 0.03

rows = c.execute("""SELECT id, date, symbol, direction d, entry_price e, entry_low lo, entry_high hi,
                           raw_instruction_json raw, execution_price xp, executed x, atr_pct a
                    FROM playbook_instructions
                    WHERE entry_low>0 AND entry_high>0 AND entry_price>0 ORDER BY id""").fetchall()
print(f"样本 n={len(rows)}")

pairs = []       # (反推锚, planned_entry, 偏差%, entry_price, 反推锚vs entry偏差%)
no_planned = 0
for r in rows:
    try:
        raw = json.loads(r["raw"]) if isinstance(r["raw"], str) and r["raw"] else {}
    except Exception:
        raw = {}
    if not isinstance(raw, dict): raw = {}
    pe = raw.get("planned_entry")
    if not (isinstance(pe, (int, float)) and float(pe) > 0):
        # 也试嵌套
        for k in ("planned_entry", "entry"):
            v = raw.get(k)
            if isinstance(v, dict):
                vv = v.get("price") or v.get("entry_price")
                if isinstance(vv, (int, float)) and float(vv) > 0:
                    pe = vv; break
    e = float(r["e"]); lo = float(r["lo"]); hi = float(r["hi"])
    d = str(r["d"] or "?").upper(); short = d.startswith("S")
    anchor = lo / (1 - CHASE) if short else hi / (1 + CHASE)
    if pe is None:
        no_planned += 1
        continue
    pe = float(pe)
    dev_a = (anchor - pe) / pe * 100      # 反推锚 vs 真实生成价
    dev_e = (anchor - e) / e * 100        # 反推锚 vs DB entry_price
    pairs.append((dev_a, dev_e, anchor, pe, e, r))

print(f"有 planned_entry 可配对 n={len(pairs)}   无 planned_entry n={no_planned}")
print()
print("=" * 84)
print("判别结果")
print("=" * 84)
if pairs:
    da = sorted(abs(x[0]) for x in pairs); de = sorted(abs(x[1]) for x in pairs); k = len(da)
    print(f"  反推锚 vs raw.planned_entry (真实生成价):")
    print(f"    |偏差| 中位={st.median(da):.4f}%  p75={da[3*k//4]:.4f}%  p90={da[int(k*.9)]:.4f}%  max={da[-1]:.4f}%")
    print(f"    吻合(|偏差|<0.1%) = {sum(1 for x in da if x<0.1)}/{k} ({100*sum(1 for x in da if x<0.1)/k:.1f}%)")
    print(f"  反推锚 vs DB entry_price:")
    print(f"    |偏差| 中位={st.median(de):.4f}%  p75={de[3*k//4]:.4f}%  p90={de[int(k*.9)]:.4f}%  max={de[-1]:.4f}%")
    print(f"    吻合(|偏差|<0.1%) = {sum(1 for x in de if x<0.1)}/{k} ({100*sum(1 for x in de if x<0.1)/k:.1f}%)")
    print()
    m_a = st.median(da); m_e = st.median(de)
    if m_a < m_e / 3:
        print(f"  ⇒ 解释(a) 成立: band 是 compute_entry_band 输出(反推锚≈真实生成价),")
        print(f"     DB entry_price 已被 reprice 污染 ⇒ **RFC-123 生命周期混用有硬数据支撑**")
        print(f"     量化: {100*sum(1 for x in de if x>=0.1)/k:.1f}% 的指令 entry_price ≠ 生成价")
    elif m_e < m_a / 3:
        print(f"  ⇒ 解释(b) 成立: band 与生成价无关, entry_price 才是锚 ⇒ band 是散文位(T4 契约正确)")
    else:
        print(f"  ⇒ 两者都不干净: 反推锚既不等于生成价也不等于 entry_price")
        print(f"     ⇒ band 可能由第三套逻辑产出 (需查 DA skill 侧 _compute_entry_band 实现)")
    print()
    print("  --- 偏差最大的 8 笔 (看形态) ---")
    for dev_a, dev_e, anchor, pe, e, r in sorted(pairs, key=lambda x: -abs(x[0]))[:8]:
        print(f"    {r['symbol']:9s} {r['date']} {str(r['d'])[:9]:10s} planned_entry={pe:>9.2f} "
              f"反推锚={anchor:>9.2f} (Δ{dev_a:+.2f}%)  DB entry={e:>9.2f} (Δ{dev_e:+.2f}%) exec={r['xp']}")

print()
print("=" * 84)
print("补: entry_price 被 reprice 覆盖的直接证据 (execution_price vs entry_price)")
print("=" * 84)
rr = c.execute("""SELECT entry_price e, execution_price x FROM playbook_instructions
                  WHERE executed=1 AND entry_price>0 AND execution_price>0""").fetchall()
if rr:
    dv = sorted(abs((float(r["x"]) - float(r["e"])) / float(r["e"]) * 100) for r in rr)
    k = len(dv)
    print(f"  n={k}  |exec-entry|/entry 中位={st.median(dv):.4f}% p90={dv[int(k*.9)]:.4f}% max={dv[-1]:.4f}%")
    print(f"  ⇒ 若中位≈0 则 entry_price 已≈执行价 (reprice 后回写过) = RFC-123 描述的混用形态")
