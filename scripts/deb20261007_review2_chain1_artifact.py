#!/opt/ftseek/FTSeek-US/venv/bin/python3
"""[DEB-20261007 复查第2轮] 链①伪影判别: band 宽窄↔触发率的方向性是不是 reprice 污染产物?

背景 (自曝#11):
  §4 链① / E12 / S1 用 w=(entry_high-entry_low)/entry_price 分桶, 得「6-8% 触发率 67.8%
  > 8-10% 52.9% ⇒ 越宽越难触发」。但 entry_price 被盘中 reprice 回写 (pipeline.py
  _reprice_params + db.py _TRACKING_FIELDS 白名单), 且 reprice 只发生在触发链上
  ⇒ e≠planned 本身就是「已触发」的标记, 分桶 = 触发标记的投影。

判别法 (只读, DB mode=ro):
  ① 用真生成锚 raw.planned_entry 重算真宽度 (hi-lo)/planned — 若恒为同值(扁平带),
     则「宽度差异」全部来自分母污染;
  ② 按 sign(entry_price vs planned_entry) 分群看触发率 — 若 e≠planned 群触发率≈100%
     而 e==planned 群极低, 则桶间触发率差 = 污染比例的差, 非宽度效应。

复现: cd /opt/ftseek/FTSeek-US && venv/bin/python scripts/deb20261007_review2_chain1_artifact.py
作者实跑值 (2026-10-07): 真宽度=8.0% 恒值(605/605); e>planned 触发率 99.3% (n=279),
  e<planned 99.2% (n=121), e==planned 11.2% (n=205) ⇒ 链①方向性 = 伪影, S1「反向证据」不成立。
"""
import sqlite3, json, collections

c = sqlite3.connect("file:data/ftseek_us.db?mode=ro", uri=True)
c.row_factory = sqlite3.Row
rows = c.execute("""SELECT id, symbol, date, entry_low lo, entry_high hi, entry_price e,
                           triggered t, executed x, raw_instruction_json raw
                    FROM playbook_instructions WHERE entry_low>0 AND entry_high>0 AND entry_price>0
                    ORDER BY id""").fetchall()
print(f"n={len(rows)}")

stats = collections.Counter()
bucket_by_sign = collections.defaultdict(lambda: [0, 0, 0])  # sign(e-planned) -> [n, trig, exec]
widths_true = []
for r in rows:
    lo, hi, e = float(r["lo"]), float(r["hi"]), float(r["e"])
    w = (hi - lo) / e * 100
    b = "<4%" if w < 4 else "4-6%" if w < 6 else "6-8%" if w < 8 else "8-10%" if w < 10 else ">=10%"
    pe = None
    try:
        raw = json.loads(r["raw"]) if isinstance(r["raw"], str) else (r["raw"] or {})
        if isinstance(raw, dict):
            pe = raw.get("planned_entry")
    except Exception:
        pe = None
    if pe:
        pe = float(pe)
        widths_true.append(round((hi - lo) / pe * 100, 2))
        sgn = "e>planned" if e > pe * 1.001 else "e<planned" if e < pe * 0.999 else "e==planned"
        stats[(b, sgn)] += 1
        bucket_by_sign[sgn][0] += 1
        bucket_by_sign[sgn][1] += int(bool(r["t"]))
        bucket_by_sign[sgn][2] += int(bool(r["x"]))
    else:
        stats[(b, "no_planned")] += 1

print("\n真宽度 (hi-lo)/planned 分布:", collections.Counter(widths_true).most_common(8))
print("⇒ 若只有单一值(扁平带), 则显示桶的宽度差异全部来自分母 entry_price 污染")

print("\n显示桶 × sign(entry_price vs planned):")
for k in sorted(stats):
    print("  ", k, stats[k])

print("\n按 sign 的触发率 (reprice 只发生在触发链上 ⇒ e≠planned ≈ 已触发标记):")
for sgn, (n, t, x) in sorted(bucket_by_sign.items()):
    print(f"  {sgn:12s} n={n:4d} 触发率={100*t/n:.1f}% 执行率={100*x/n:.1f}%")

nop = collections.Counter(k[0] for k, v in stats.items() if k[1] == "no_planned" for _ in range(v))
print("\n无 planned 行的桶分布:", dict(nop))
print("\n⇒ 判读: e≠planned 两群触发率≈99% 而 e==planned 群≈11% ⇒ 桶间触发率差由污染比例驱动,")
print("   「越宽越难触发」方向性 = 伪影 (自曝#11)。S1 的真正状态 = 不可测 (历史全扁平带, 无真实宽度变异)。")
