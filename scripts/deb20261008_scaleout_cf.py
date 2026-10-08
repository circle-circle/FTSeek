#!/usr/bin/env python
"""DEB 粗筛反事实 — 2%/3%/4% 分批一半 vs 现状全仓持有.

⚠️ 层次声明 (skill exit-parameter-validation「三个模拟层次」):
   本脚本是 **MAE 近似级粗筛**, 只用于方向判断, **不可定稿**。
   两处已知乐观偏差必须并列披露:
     ① 假设「摸到阈值即能在阈值价成交一半」— bar 触及回放系统性高估触发率
        (skill 记载: 62% vs 实际 22 笔);
     ② 未模拟分批后剩余半仓的路径变化 (假设与原路径同).
   定稿须轮询级模拟 (playbook_exit_fast_loop_interval_sec=10, 从 config 核实)。

公式 (逐笔, 名义归一化):
   baseline_pct = 实际落袋 pnl_pct (小数比例 → 百分点)
   ladder_pct   = 0.5×阈值 + 0.5×baseline_pct − 增量费用bp/100
   Δ_bp         = (ladder_pct − baseline_pct) × 100 = 0.5×(阈值 − baseline)×100 − 增量费用bp

只读 (mode=ro), 零写入。
用法: cd /opt/ftseek/FTSeek-US && venv/bin/python scripts/deb20261008_scaleout_cf.py
"""
from pathlib import Path as _P
_ROOT = str(_P(__file__).resolve().parents[1])

import sqlite3
import statistics as st
import sys

sys.path.insert(0, _ROOT)
from app.execution.fee_model import get_schedule  # noqa: E402

SCHED = get_schedule()
MIN_COMM = SCHED["commission_min_per_order"]
PER_SHARE = SCHED["commission_per_share"]
SETTLE_PS = SCHED["settlement_per_share"]

c = sqlite3.connect(f"file:{_ROOT}/data/ftseek_us.db?mode=ro", uri=True)
c.row_factory = sqlite3.Row
rows = c.execute(
    "SELECT id, symbol, trade_date, direction, entry_price, quantity, pnl_net, pnl_pct, "
    "       mfe_pct, fees_est, exit_reason "
    "FROM trade_records WHERE exit_time IS NOT NULL"
).fetchall()

recs = []
for r in rows:
    if r["mfe_pct"] is None or r["pnl_pct"] is None or not r["quantity"] or not r["entry_price"]:
        continue
    qty = float(r["quantity"])
    notional = qty * float(r["entry_price"])
    if notional <= 0:
        continue
    q1, q2 = qty // 2, qty - qty // 2
    one = max(PER_SHARE * qty, MIN_COMM) + SETTLE_PS * qty
    two = (max(PER_SHARE * q1, MIN_COMM) + SETTLE_PS * q1
           + max(PER_SHARE * q2, MIN_COMM) + SETTLE_PS * q2)
    recs.append({
        "id": r["id"], "sym": r["symbol"], "d": r["trade_date"], "dir": r["direction"],
        "mfe": float(r["mfe_pct"]),                     # 百分点
        "base": float(r["pnl_pct"]) * 100.0,            # 小数比例 → 百分点
        "net": float(r["pnl_net"]) if r["pnl_net"] is not None else 0.0,
        "notional": notional, "er": str(r["exit_reason"] or "NULL"),
        "extra_bp": 10000.0 * (two - one) / notional,   # 分批增量费用 bp
    })

print(f"样本 n={len(recs)}")
print(f"分批增量费用: 中位={st.median([x['extra_bp'] for x in recs]):.1f}bp  "
      f"均值={st.mean([x['extra_bp'] for x in recs]):.1f}bp")


def day_cluster_t(items):
    """日聚类 t: 分母含全部交易日 (零差异日贡献 0), skill 纪律第6条."""
    by = {}
    for x in items:
        by.setdefault(x["d"], []).append(x["delta"])
    days = [st.mean(v) for v in by.values()]
    if len(days) < 2:
        return 0.0, len(days)
    mu = st.mean(days)
    sd = st.stdev(days)
    return (mu / (sd / len(days) ** 0.5) if sd else 0.0), len(days)


print("\n" + "=" * 96)
print("粗筛反事实: 摸到阈值即出一半, 剩余半仓按实际路径 (MAE 近似级, 方向判断用)")
print("=" * 96)
print(f"  {'阈值':>6s} {'触发n':>6s} {'Δbp中位':>9s} {'Δbp均值':>9s} {'Σ$净':>11s} "
      f"{'更好':>5s} {'更差':>5s} {'持平':>5s} {'一致性':>7s} {'日聚类t':>8s} {'n_days':>6s}")
for th in (1.0, 1.5, 2.0, 2.5, 3.0, 4.0):
    grp = [x for x in recs if x["mfe"] >= th]
    if not grp:
        continue
    for x in grp:
        ladder = 0.5 * th + 0.5 * x["base"] - x["extra_bp"] / 100.0
        x["delta"] = ladder - x["base"]                       # 百分点
        x["delta_usd"] = x["delta"] / 100.0 * x["notional"]   # 美元
    better = sum(1 for x in grp if x["delta"] > 0.001)
    worse = sum(1 for x in grp if x["delta"] < -0.001)
    flat = len(grp) - better - worse
    consist = 100.0 * better / len(grp)
    t, nd = day_cluster_t(grp)
    tot = sum(x["delta_usd"] for x in grp)
    print(f"  {th:5.1f}% {len(grp):6d} {st.median([x['delta'] for x in grp])*100:8.1f} "
          f"{st.mean([x['delta'] for x in grp])*100:8.1f} ${tot:10,.2f} "
          f"{better:5d} {worse:5d} {flat:5d} {consist:6.1f}% {t:8.2f} {nd:6d}")

print("\n" + "=" * 96)
print("门槛对照 (预注册判据同族: 一致性≥55% + n≥30 + 日聚类|t|≥2)")
print("=" * 96)
print("  一致性 = 更好笔数 / 触发笔数; 毕业门 = 55% (exit_shadow_graduation.py 权威入口)")
print("  ⇒ 一致性 <55% = 均值正也是「总账幻觉」(少数尾部撑起), 禁翻开关")

for th in (2.0, 3.0):
    grp = [x for x in recs if x["mfe"] >= th]
    for x in grp:
        ladder = 0.5 * th + 0.5 * x["base"] - x["extra_bp"] / 100.0
        x["delta_usd"] = (ladder - x["base"]) / 100.0 * x["notional"]
    grp.sort(key=lambda z: z["delta_usd"])
    print(f"\n  --- 阈值 {th}%: 最受伤的 5 笔 (阶梯把大赢家提前卖半) ---")
    for x in grp[:5]:
        print(f"    {x['sym']:10s} {x['d']} {x['dir'][:9]:9s} MFE={x['mfe']:6.2f}% "
              f"实际落袋={x['base']:6.2f}%  Δ=${x['delta_usd']:9,.2f}  出场={x['er']}")
    print(f"  --- 最受益的 3 笔 ---")
    for x in grp[-3:]:
        print(f"    {x['sym']:10s} {x['d']} {x['dir'][:9]:9s} MFE={x['mfe']:6.2f}% "
              f"实际落袋={x['base']:6.2f}%  Δ=${x['delta_usd']:9,.2f}  出场={x['er']}")
    tot = sum(x["delta_usd"] for x in grp)
    top3 = sum(x["delta_usd"] for x in grp[-3:])
    print(f"  ⇒ Σ=${tot:,.2f}  剔最受益3笔后 Σ=${tot-top3:,.2f}  "
          f"(Top-3 占 {100*top3/tot:.0f}% )" if tot else f"  ⇒ Σ=${tot:,.2f}")

print("\n" + "=" * 96)
print("结论行 (供 DEB 引用)")
print("=" * 96)
for th in (2.0, 3.0, 4.0):
    grp = [x for x in recs if x["mfe"] >= th]
    for x in grp:
        ladder = 0.5 * th + 0.5 * x["base"] - x["extra_bp"] / 100.0
        x["delta_usd"] = (ladder - x["base"]) / 100.0 * x["notional"]
    better = sum(1 for x in grp if x["delta_usd"] > 0.01)
    worse = sum(1 for x in grp if x["delta_usd"] < -0.01)
    print(f"  阈值{th:.0f}%: n={len(grp)}  Σ$={sum(x['delta_usd'] for x in grp):+,.2f}  "
          f"更好={better} 更差={worse}  一致性={100*better/max(len(grp),1):.1f}%")
