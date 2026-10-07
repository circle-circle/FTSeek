#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""⑱ forward 窗 (2026-09-30 起) 的 G3 动态费用缺口实值 + vol_widen 影子事件语义.
   G3 是 6 门里唯一可能因门槛变化而翻的门 ⇒ 必须按 forward 窗自己算, 不用全窗."""
from pathlib import Path as _P
_ROOT = str(_P(__file__).resolve().parents[1])
import sqlite3, json, collections, statistics as st
c = sqlite3.connect(f"file:{_ROOT}/data/ftseek_us.db?mode=ro", uri=True); c.row_factory = sqlite3.Row

print("=" * 88)
print("⑱-1  G3 动态门槛: forward 窗 (>=2026-09-30) 的费用缺口 bp/笔")
print("=" * 88)
print("  口径: 缺口 = (费用/笔 − 毛利/笔) ÷ 名义/笔 ×10000  (推送层同屏动态算)")
for label, since in (("forward (>=09-30)", "2026-09-30"), ("9月全月", "2026-09-01"), ("全历史", "2000-01-01")):
    r = c.execute("""SELECT fees_est f, realized_pnl g, entry_price ep, quantity q FROM trade_records
                     WHERE trade_date>=? AND fees_est>0 AND entry_price>0 AND quantity>0""", (since,)).fetchall()
    if not r:
        print(f"  {label:22s} n=0"); continue
    notional = sum(float(x["ep"]) * float(x["q"]) for x in r)
    fee = sum(float(x["f"]) for x in r); gross = sum(float(x["g"] or 0) for x in r)
    n = len(r)
    fee_bp = fee / notional * 10000; gross_bp = gross / notional * 10000
    gap = (fee / n - gross / n) / (notional / n) * 10000
    print(f"  {label:22s} n={n:4d} 名义=${notional:>12,.0f}  费用={fee_bp:6.1f}bp  毛利={gross_bp:6.1f}bp  "
          f"缺口={gap:+6.1f}bp")
print()
print("  ⇒ G3 判据: bp_per_trade(限价臂改善) ≥ 费用缺口")
print("     θ=0.25 腿实测 −0.3bp | θ=0.5 腿实测 −8.6bp")
print("     若 forward 缺口为负(毛利已覆盖费用) ⇒ 门槛≈0, 两腿仍 FAIL (负值 < 0)")

print()
print("=" * 88)
print("⑱-2  RFC-065 vol_widen 影子事件语义 (38 行是什么事件)")
print("=" * 88)
rows = [json.loads(l) for l in open(f"{_ROOT}/data/entry_band_widen_shadow.jsonl", encoding="utf-8") if l.strip()]
print(f"  行数 n={len(rows)}")
wp = collections.Counter(str(r.get("widen_path")) for r in rows)
fp = collections.Counter(str(r.get("flat_path")) for r in rows)
ft = collections.Counter(str(r.get("flat_triggered")) for r in rows)
print(f"  widen_path 分布: {dict(wp.most_common(8))}")
print(f"  flat_path  分布: {dict(fp.most_common(8))}")
print(f"  flat_triggered 分布: {dict(ft)}")
dt = sorted(set(str(r.get("date"))[:10] for r in rows))
print(f"  日期范围 {dt[0]}~{dt[-1]}  独立日={len(dt)}")
kn = sorted(float(r.get("widen_knife_pct") or 0) for r in rows)
ch = sorted(float(r.get("widen_chase_pct") or 0) for r in rows)
if kn:
    print(f"  widen_knife_pct: 中位={st.median(kn):.2f}% min={kn[0]:.2f}% max={kn[-1]:.2f}%")
    print(f"  widen_chase_pct: 中位={st.median(ch):.2f}% min={ch[0]:.2f}% max={ch[-1]:.2f}%")
vg = collections.Counter(str(r.get("vol_gate_block")) for r in rows)
print(f"  vol_gate_block 分布: {dict(vg.most_common(6))}")
# 判据: widen 专属事件 = widen 触发而 flat 未触发
excl = [r for r in rows if r.get("widen_path") and not r.get("flat_triggered")]
print(f"\n  widen专属事件(widen有路径 且 flat未触发) n={len(excl)}")
print(f"  ⇒ 判据 n≥30 (RFC-065) / 注册表 min_n=60 ⇒ 当前 {len(rows)} 行 "
      f"{'达标' if len(rows)>=60 else '未达标 (差 '+str(60-len(rows))+' 行)'}")
print(f"  ⇒ 生产开关 entry_band_vol_widen_enabled=false (config/unified_config.yaml:1254) ⇒ 影子攒样中, 不可翻")

print()
print("=" * 88)
print("⑱-3  成交率瓶颈归因 (定价 vs 上游供给 vs 资金护栏)")
print("=" * 88)
tot = c.execute("SELECT COUNT(*) FROM playbook_instructions").fetchone()[0]
tr = c.execute("SELECT COUNT(*) FROM playbook_instructions WHERE triggered=1").fetchone()[0]
ex = c.execute("SELECT COUNT(*) FROM playbook_instructions WHERE executed=1").fetchone()[0]
un = tot - tr
print(f"  指令 {tot} → 触发 {tr} ({100*tr/tot:.1f}%) → 执行 {ex} ({100*ex/tot:.1f}%)")
print(f"\n  漏斗损耗分解:")
sr = c.execute("""SELECT skip_reason s, COUNT(*) n FROM playbook_instructions
                  WHERE (triggered IS NULL OR triggered=0) GROUP BY skip_reason ORDER BY n DESC LIMIT 5""").fetchall()
for r in sr:
    lab = str(r["s"] or "(空)")[:44]
    kind = "上游供给(非定价)" if "非当前通道候选" in lab else ("定价/触发几何" if "no_trigger" in lab or "未触发" in lab else "信号/护栏")
    print(f"    未触发: {lab:46s} {r['n']:5d} ({100*r['n']/tot:5.1f}% of 全指令)  ⟵ {kind}")
print()
# 🔴 DEB-20261007 §11 自曝#3 根因修复 (2026-10-07): 原为 `ORDER BY n DESC LIMIT 6`,
#    对截断后的 6 行求和 ⇒ 32 条 chase_cap_enforced 全被漏掉, 报出「定价门 0/94 = 0.0%」。
#    修法 = 删 LIMIT 走全量聚合 (减代码消除根因, 非加校验打补丁)。
#    全量重算真值 = 33/94 = 35.1%; 交叉验证见 scripts/deb20261007_funnel_breakdown.py (E20)。
sr2 = c.execute("""SELECT skip_reason s, COUNT(*) n FROM playbook_instructions
                   WHERE triggered=1 AND (executed IS NULL OR executed=0)
                   GROUP BY skip_reason ORDER BY n DESC""").fetchall()
tn = c.execute("SELECT COUNT(*) FROM playbook_instructions WHERE triggered=1 AND (executed IS NULL OR executed=0)").fetchone()[0]
for r in sr2:
    lab = str(r["s"] or "(空)")[:44]
    kind = "资金/时段/执行" if "trigger_no_execution" in lab else ("定价门(chase/knife)" if "chase_cap" in lab or "knife_gate" in lab else "信号失效")
    print(f"    已触发未执行: {lab:40s} {r['n']:4d} ({100*r['n']/max(tn,1):5.1f}% of 未执行)  ⟵ {kind}")
print(f"\n  ⇒ 定价门(chase_cap/knife)在「已触发未执行」中占比: "
      f"{sum(r['n'] for r in sr2 if 'chase_cap' in str(r['s']) or 'knife_gate' in str(r['s']))}/{tn} = "
      f"{100*sum(r['n'] for r in sr2 if 'chase_cap' in str(r['s']) or 'knife_gate' in str(r['s']))/max(tn,1):.1f}%")
