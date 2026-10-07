#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""⑲ 修正自曝: 漏斗损耗用 LIMIT 6 截断列表求和 ⇒ 「定价门 0/94」错误. 全量重算.
   + 核 RFC-065 判据 n≥30 vs 注册表 min_n=60 的口径分歧."""
from pathlib import Path as _P
_ROOT = str(_P(__file__).resolve().parents[1])
import sqlite3, collections
c = sqlite3.connect(f"file:{_ROOT}/data/ftseek_us.db?mode=ro", uri=True); c.row_factory = sqlite3.Row

print("=" * 90)
print("⑲-1  「已触发未执行」全量归因 (无 LIMIT, 按门族聚合)")
print("=" * 90)
tn = c.execute("SELECT COUNT(*) FROM playbook_instructions WHERE triggered=1 AND (executed IS NULL OR executed=0)").fetchone()[0]
rows = c.execute("""SELECT skip_reason s, COUNT(*) n FROM playbook_instructions
                    WHERE triggered=1 AND (executed IS NULL OR executed=0)
                    GROUP BY skip_reason ORDER BY n DESC""").fetchall()
print(f"  已触发未执行 全量 n={tn}")
fam = collections.Counter()
print(f"\n  {'skip_reason':56s} {'n':>4s} {'占比':>7s} {'门族':>20s}")
for r in rows:
    s = str(r["s"] or "(空)")
    if "chase_cap" in s or "knife_gate" in s: f = "定价门(chase/knife)"
    elif "falsification_slope" in s or "vol_gate" in s or "vwap" in s: f = "证伪/量能门"
    elif "trigger_no_execution" in s: f = "资金/时段/执行失败"
    elif "notional" in s: f = "资金帽"
    elif "thesis_drift" in s or "TRIPWIRE" in s: f = "论点/三振线失效"
    elif "cooldown" in s: f = "冷却期"
    elif s in ("", "None"): f = "(未标注)"
    else: f = "其他/数据缺失"
    fam[f] += r["n"]
    print(f"  {s[:54]:56s} {r['n']:4d} {100*r['n']/max(tn,1):6.1f}% {f:>20s}")
print(f"\n  --- 按门族聚合 ---")
for k, v in fam.most_common():
    print(f"    {k:26s} {v:4d} ({100*v/max(tn,1):5.1f}%)")
price_n = fam.get("定价门(chase/knife)", 0)
print(f"\n  🔴 自曝修正: 上一版报「定价门 0/94 = 0.0%」是**用 LIMIT 6 截断列表求和**的伪结果")
print(f"     全量重算: 定价门(chase_cap/knife_gate) = {price_n}/{tn} = {100*price_n/max(tn,1):.1f}%")

print()
print("=" * 90)
print("⑲-2  定价门明细 (chase_cap_enforced 的具体形态)")
print("=" * 90)
rr = c.execute("""SELECT symbol, date, direction, skip_reason s FROM playbook_instructions
                  WHERE triggered=1 AND (executed IS NULL OR executed=0)
                    AND (skip_reason LIKE '%chase_cap%' OR skip_reason LIKE '%knife_gate%')
                  ORDER BY date DESC""").fetchall()
for r in rr:
    print(f"  {r['date']} {r['symbol']:9s} {str(r['direction'])[:9]:10s} {str(r['s'])[:70]}")
print(f"  合计 {len(rr)} 笔")

print()
print("=" * 90)
print("⑲-3  RFC-065 判据口径分歧: 文档 n≥30 vs 注册表 min_n=60")
print("=" * 90)
import re
src = open("/opt/ftseek/FTSeek-US/app/playbook/shadow_registry.py", encoding="utf-8").read()
i = src.find('"entry_band_widen"')
if i < 0: i = src.find("entry_band_widen")
seg = src[i-200:i+1800] if i > 0 else ""
for kw in ("min_n", "criteria", "判据", "n≥", "n>="):
    for m in re.finditer(re.escape(kw), seg):
        line = seg[max(0, m.start()-90):m.start()+220].replace("\n", " ")
        print(f"  [{kw}] ...{line}...")
        break
print()
print("  --- 文档侧判据原文 (RFC-065 经子代理逐字提取) ---")
print("  「翻开关判据预注册: widen专属事件 n≥30 + 单侧安全门(widen组不显著劣于flat)")
print("    + 分组事先固定 + 分时间窗; 不满足则影子顺延不凭中途印象翻开关」")
print()
print("  ⇒ 分歧: 文档 n≥30, 注册表 min_n=60")
print("     当前 widen 专属事件 n=38 ⇒ 按文档口径**已过 n 门**, 按注册表口径**未过**(差22)")
print("     ⚠ 但文档判据是合取: n≥30 ∧ 单侧安全门 ∧ 分组事先固定 ∧ 分时间窗")
print("        ⇒ n 达标不等于判据达标 (review-system-design 铁律: READY≠达标)")
