# -*- coding: utf-8 -*-
"""DA 定价质量实证: DA planned 几何 vs 结局的预测力 — 决定"反馈优化"是否值得.

Q1: DA planned SL距 是否遵守 prompt 要求的 2-5% 带? 分布如何?
Q2: DA planned RR 分布? prompt 教 RR 了吗?
Q3: DA planned SL距 / RR 对结局 (pnl_net, mfe=0, SL_INITIAL) 有无预测力?
    → 有预测力 = DA 范围含信息, 值得反馈/直通; 无 = clamp 机械重算损失为零
Q4: re-price 幅度 (|落库entry/DA entry−1|) 对结局有无预测力? (追价距离税)
"""
import argparse
import json
import sqlite3
import statistics as st

ap = argparse.ArgumentParser(description="DEB-20260916 §七 预测力检验 (record-only)")
ap.add_argument("--since", default="2026-08-01", help="指令窗口起点 (默认 2026-08-01)")
args = ap.parse_args()
SINCE = args.since

db = sqlite3.connect("file:/opt/ftseek/FTSeek-US/data/ftseek_us.db?mode=ro", uri=True)
db.row_factory = sqlite3.Row

rows = db.execute(
    """SELECT i.date, i.symbol, i.direction, i.entry_price, i.stop_loss, i.take_profit,
              i.raw_instruction_json, t.pnl_net, t.exit_reason, t.mfe_pct
       FROM playbook_instructions i
       JOIN trade_records t ON t.decision_id = i.decision_id AND t.source_type='PLAYBOOK_SIM'
       WHERE i.date >= ? AND i.direction IN ('BUY','SELL_SHORT')
         AND i.entry_price > 0 AND t.pnl_net IS NOT NULL""", (SINCE,)).fetchall()

recs = []
for r in rows:
    try:
        raw = json.loads(r["raw_instruction_json"] or "{}")
    except Exception:
        continue
    pe, ps, pt = raw.get("planned_entry"), raw.get("planned_stop_loss"), raw.get("planned_take_profit")
    if not all(isinstance(x, (int, float)) and x > 0 for x in (pe, ps, pt)):
        continue
    short = r["direction"] == "SELL_SHORT"
    risk = abs(ps - pe) / pe * 100
    rew = (pe - pt) if short else (pt - pe)
    rr = rew / abs(ps - pe) if abs(ps - pe) > 0 else None
    chase = abs(r["entry_price"] / pe - 1) * 100
    recs.append({
        "sym": r["symbol"].split(".")[-1], "date": r["date"], "dir": "S" if short else "L",
        "sl_pct": risk, "rr": rr, "chase_pct": chase,
        "pnl": r["pnl_net"], "exit": r["exit_reason"],
        "mfe0": (r["mfe_pct"] is not None and abs(r["mfe_pct"]) < 1e-9),
    })

print(f"可配对已平仓样本 (8/1起): n={len(recs)}")

print("\n=== Q1: DA planned SL距 分布 vs prompt 要求的 2-5% 带 ===")
s = [x["sl_pct"] for x in recs]
inband = sum(1 for v in s if 2.0 <= v <= 5.0)
print(f"中位 {st.median(s):.2f}% P25 {sorted(s)[len(s)//4]:.2f}% P75 {sorted(s)[len(s)*3//4]:.2f}% "
      f"min {min(s):.2f}% max {max(s):.2f}%")
print(f"落在 2-5% 带内: {inband}/{len(s)} = {inband/len(s)*100:.0f}%")
print(f"<2%: {sum(1 for v in s if v<2)} | >5%: {sum(1 for v in s if v>5)}")

print("\n=== Q2: DA planned RR 分布 ===")
rrs = [x["rr"] for x in recs if x["rr"] is not None and x["rr"] < 50]
print(f"中位 {st.median(rrs):.2f} P25 {sorted(rrs)[len(rrs)//4]:.2f} P75 {sorted(rrs)[len(rrs)*3//4]:.2f}")
print(f"RR≥1.5 (执行层门禁线): {sum(1 for v in rrs if v>=1.5)}/{len(rrs)} = {sum(1 for v in rrs if v>=1.5)/len(rrs)*100:.0f}%")

print("\n=== Q3: DA SL距 分桶 → 结局 (预测力检验) ===")
print(f"{'桶':12s} {'n':>4s} {'净$':>9s} {'均$':>7s} {'WR':>5s} {'mfe=0率':>7s} {'SL_INIT':>7s}")
for lo, hi, name in [(0, 2, "<2%"), (2, 3, "2-3%"), (3, 4, "3-4%"), (4, 5, "4-5%"), (5, 99, "≥5%")]:
    b = [x for x in recs if lo <= x["sl_pct"] < hi]
    if not b:
        continue
    pn = [x["pnl"] for x in b]
    print(f"{name:12s} {len(b):4d} {sum(pn):+9.0f} {st.mean(pn):+7.1f} "
          f"{sum(1 for v in pn if v>0)/len(pn)*100:4.0f}% "
          f"{sum(1 for x in b if x['mfe0'])/len(b)*100:6.0f}% "
          f"{sum(1 for x in b if x['exit']=='SL_INITIAL')/len(b)*100:6.0f}%")

print("\n=== Q3b: DA RR 分桶 → 结局 ===")
print(f"{'桶':12s} {'n':>4s} {'净$':>9s} {'均$':>7s} {'WR':>5s} {'mfe=0率':>7s}")
for lo, hi, name in [(0, 1, "RR<1"), (1, 1.5, "1-1.5"), (1.5, 2.5, "1.5-2.5"), (2.5, 5, "2.5-5"), (5, 99, "≥5")]:
    b = [x for x in recs if x["rr"] is not None and lo <= x["rr"] < hi]
    if not b:
        continue
    pn = [x["pnl"] for x in b]
    print(f"{name:12s} {len(b):4d} {sum(pn):+9.0f} {st.mean(pn):+7.1f} "
          f"{sum(1 for v in pn if v>0)/len(pn)*100:4.0f}% "
          f"{sum(1 for x in b if x['mfe0'])/len(b)*100:6.0f}%")

print("\n=== Q4: 追价距离 |落库entry/DA entry−1| 分桶 → 结局 ===")
print(f"{'桶':12s} {'n':>4s} {'净$':>9s} {'均$':>7s} {'WR':>5s} {'mfe=0率':>7s} {'SL_INIT':>7s}")
for lo, hi, name in [(0, 0.2, "≈0(未追)"), (0.2, 1, "0.2-1%"), (1, 2, "1-2%"), (2, 99, "≥2%")]:
    b = [x for x in recs if lo <= x["chase_pct"] < hi]
    if not b:
        continue
    pn = [x["pnl"] for x in b]
    print(f"{name:12s} {len(b):4d} {sum(pn):+9.0f} {st.mean(pn):+7.1f} "
          f"{sum(1 for v in pn if v>0)/len(pn)*100:4.0f}% "
          f"{sum(1 for x in b if x['mfe0'])/len(b)*100:6.0f}% "
          f"{sum(1 for x in b if x['exit']=='SL_INITIAL')/len(b)*100:6.0f}%")

# 简单相关性 (Spearman 用秩相关近似)
def spearman(xs, ys):
    n = len(xs)
    if n < 8:
        return None
    rx = {v: i for i, v in enumerate(sorted(xs))}
    ry = {v: i for i, v in enumerate(sorted(ys))}
    dx = [rx[v] - (n-1)/2 for v in xs]
    dy = [ry[v] - (n-1)/2 for v in ys]
    num = sum(a*b for a, b in zip(dx, dy))
    den = (sum(a*a for a in dx) * sum(b*b for b in dy)) ** 0.5
    return num/den if den else None

print("\n=== 秩相关 (Spearman, |ρ|>0.15 且 n>30 才算有信号) ===")
print(f"DA SL距 vs pnl:      ρ={spearman([x['sl_pct'] for x in recs], [x['pnl'] for x in recs]):+.3f}")
r2 = [x for x in recs if x["rr"] is not None and x["rr"] < 50]
print(f"DA RR vs pnl:        ρ={spearman([x['rr'] for x in r2], [x['pnl'] for x in r2]):+.3f}")
print(f"追价距离 vs pnl:     ρ={spearman([x['chase_pct'] for x in recs], [x['pnl'] for x in recs]):+.3f}")
print(f"追价距离 vs mfe=0:   ρ={spearman([x['chase_pct'] for x in recs], [1.0 if x['mfe0'] else 0.0 for x in recs]):+.3f}")
