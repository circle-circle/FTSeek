"""第十六轮 (落地性决定性): low_atr_gate 的 atr_pct 与 metadata.atr 是否同值?

矛盾: 门拦 atr_pct < 0.03 (比例=3%), 但 SHORT × metadata.atr<3 (百分数=3%)
      在门上线 (7/24) 后仍有 19 笔成交 (净 +699.2)。
两种解释, 落地性完全相反:
  (a) 门读的 inst.atr_pct 是**止损口径** (含VIX缩放/三层底限/分钟兜底)
      ⇒ 低波票被抬高到 >0.03 ⇒ 门对它们**不生效** ⇒ SHORT×低波腿现在活着, 可落地
  (b) 门只作用于某些路径 (如 DA trigger 侧), UE/reentry 不过门
      ⇒ 该腿活在门覆盖外, 落地需先确认覆盖范围
查 playbook_instructions.atr_pct 与 trade_records.metadata.atr 的逐笔对照。
"""
import json
import sqlite3

BASE = "/opt/ftseek/FTSeek-US"
c = sqlite3.connect(f"file:{BASE}/data/ftseek_us.db?mode=ro", uri=True)
c.row_factory = sqlite3.Row

# 1. playbook_instructions 有无 atr_pct 列
cols = [r["name"] for r in c.execute("PRAGMA table_info(playbook_instructions)")]
print("=== playbook_instructions 列 (找 atr 相关) ===")
print(f"  {[x for x in cols if 'atr' in x.lower() or 'vol' in x.lower() or 'stop' in x.lower()]}")

# 2. 门上线后 SHORT 低波成交笔, 对照其指令行的 atr_pct
tr = c.execute(
    "SELECT symbol, direction, entry_time, entry_price, metadata_json, decision_id, "
    " pnl_net, mfe_pct FROM trade_records "
    "WHERE entry_time>='2026-07-24' AND exit_time IS NOT NULL AND pnl_net IS NOT NULL "
    "AND direction='SHORT'").fetchall()

print("\n=== 门上线后 SHORT 成交笔: metadata.atr vs 指令行 atr_pct ===")
print(f"  {'时间':17s} {'标的':11s} {'md.atr':>8s} {'inst.atr_pct':>13s} {'同值?':>7s} {'净':>8s}")
pairs, mism = [], 0
for r in tr:
    try:
        md = json.loads(r["metadata_json"]) if r["metadata_json"] else {}
    except Exception:
        md = {}
    a_md = md.get("atr")
    if a_md in (None, 0):
        continue
    did = r["decision_id"] or ""
    # 从指令行取 atr_pct
    a_inst = None
    if "atr_pct" in cols:
        row = c.execute("SELECT atr_pct FROM playbook_instructions WHERE decision_id=?",
                        (did,)).fetchone()
        if row is not None:
            a_inst = row["atr_pct"]
    same = "—" if a_inst is None else ("✅同" if abs(a_inst * 100 - a_md) < 0.5 else "🔴异")
    if a_inst is not None and abs(a_inst * 100 - a_md) >= 0.5:
        mism += 1
    if len(pairs) < 25:
        print(f"  {(r['entry_time'] or '')[:16]} {r['symbol']:11s} {a_md:7.2f}% "
              f"{(f'{a_inst:.4f}' if a_inst is not None else 'NULL'):>13s} {same:>7s} "
              f"{(r['pnl_net'] or 0):+8.1f}")
    pairs.append((a_md, a_inst))

valid = [(m, i) for m, i in pairs if i is not None]
print(f"\n  可比对 {len(valid)}/{len(pairs)} 笔; 口径不一致 {mism} 笔")
if valid:
    lo = [(m, i) for m, i in valid if m < 3]
    print(f"  其中 metadata.atr<3% 的: {len(lo)} 笔")
    for m, i in lo[:10]:
        print(f"    md={m:.2f}% inst={i:.4f} (={i*100:.2f}%) ⇒ "
              f"{'门会拦(inst<0.03)' if i < 0.03 else '门不拦(inst>=0.03)'}")
    blocked = sum(1 for m, i in lo if i < 0.03)
    print(f"  ⇒ md<3% 中 inst 也 <0.03 (门该拦): {blocked}/{len(lo)}")
    print(f"  ⇒ md<3% 中 inst >=0.03 (门不拦): {len(lo)-blocked}/{len(lo)}")

# 3. 若 atr_pct 列不存在, 找门实际读什么
if "atr_pct" not in cols:
    print("\n  ⚠️ playbook_instructions 无 atr_pct 列 ⇒ 门读的是运行时对象属性, DB 查不到")
    print("  ⇒ 只能从门代码确认口径")

# 4. 门代码: atr_pct 从哪来
print("\n=== 门代码: inst.atr_pct 的来源 (是否含缩放/底限) ===")
import re
src = open(f"{BASE}/app/playbook/pipeline.py", encoding="utf-8").read()
lines = src.split("\n")
for i, ln in enumerate(lines[5890:5975], start=5891):
    if re.search(r"atr_pct|atr\b|scale|floor|vix|min_atr", ln, re.I):
        print(f"  {i}: {ln.rstrip()[:120]}")
c.close()
