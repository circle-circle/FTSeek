#!/usr/bin/env python
"""DEB 决定性算术 — 阶梯止盈为何结构性净损 (一句话机制的量化).

机制: 阶梯「摸到阈值即出一半」的落袋 = 0.5×阈值 + 0.5×(剩余半仓按现状路径落袋) − 分批增量费用。
      若**现状落袋中位 > 阈值**, 则 0.5×阈值 < 0.5×现状落袋 ⇒ Δ 必然为负。
      ⇒ 只有「落袋 < 阈值」的笔 (摸到阈值但回吐到阈值以下) 才受益, 而那批实测仅 1 笔。

这不是参数没调好, 是「让利润奔跑」与「提前卖一半」的结构冲突:
现役机制 (SL_TRAIL_LOCK/TRAILING_STOP/EXTERNAL_CLOSE) 在大赢家上捕获率已达 100%,
阶梯把它们的一半仓位在 2%-4% 处提前卖掉。

量纲铁律: mfe_pct = 百分点; pnl_pct = 小数比例 (×100 得百分点)。
只读 (mode=ro), 零写入。
用法: cd /opt/ftseek/FTSeek-US && venv/bin/python scripts/deb20261008_scaleout_arith.py
"""
from pathlib import Path as _P
_ROOT = str(_P(__file__).resolve().parents[1])

import sqlite3
import statistics as st

EXTRA_BP = 3.4  # 分批增量费用中位 (deb_tier_scaleout_binding.py Q3 实测)

c = sqlite3.connect(f"file:{_ROOT}/data/ftseek_us.db?mode=ro", uri=True)
rows = c.execute(
    "SELECT mfe_pct, pnl_pct FROM trade_records "
    "WHERE exit_time IS NOT NULL AND mfe_pct IS NOT NULL AND pnl_pct IS NOT NULL"
).fetchall()

print(f"样本 n={len(rows)}")
print(f"分批增量费用取 {EXTRA_BP}bp (binding 脚本 Q3 实测中位)")
print()
print(f"  {'阈值':>6s} {'n':>4s} {'MFE中位':>8s} {'现状落袋中位':>12s} "
      f"{'阶梯落袋':>9s} {'Δpp':>7s}  {'落袋<阈值笔数':>13s}")
for th in (1.0, 1.5, 2.0, 2.5, 3.0, 4.0):
    g = [(m, l * 100.0) for m, l in rows if m >= th]
    if not g:
        continue
    mfe = st.median([x[0] for x in g])
    land = st.median([x[1] for x in g])
    lad = 0.5 * th + 0.5 * land - EXTRA_BP / 100.0
    below = sum(1 for _, l in g if l < th)
    print(f"  {th:5.1f}% {len(g):4d} {mfe:7.2f}% {land:11.2f}% {lad:8.2f}% "
          f"{lad - land:+6.2f}  {below:13d}")

print()
print("  ⇒ 判读: 现状落袋中位 > 阈值 ⇒ 阶梯把一半仓位在阈值价提前卖 ⇒ 中位数上 Δ 为负")
print("     受益人群 = 「摸到阈值但落袋 < 阈值」的笔 (最右列); 受损人群 = 其余 (落袋 ≥ 阈值)")
print("     ⇒ 这不是阈值没调好: 阈值越高, Δ 越负 (4% 档 −0.99pp), 因为高阈值档的现状落袋更高")

# 受益 vs 受损人群的净效应 (逐笔, 不靠中位数掩盖)
print()
print("=" * 88)
print("受益/受损人群逐笔净效应 (阈值 2.0%, 半仓, 扣 3.4bp 增量费用)")
print("=" * 88)
TH = 2.0
ben, harm = [], []
for m, l in rows:
    if m < TH:
        continue
    land = l * 100.0
    d = (0.5 * TH + 0.5 * land - EXTRA_BP / 100.0) - land   # 百分点
    (ben if d > 0 else harm).append((m, land, d))
print(f"  受益 (Δ>0) n={len(ben):3d}  Δpp 中位={st.median([x[2] for x in ben]):+.3f}  "
      f"Σ={sum(x[2] for x in ben):+.2f}pp")
print(f"  受损 (Δ≤0) n={len(harm):3d}  Δpp 中位={st.median([x[2] for x in harm]):+.3f}  "
      f"Σ={sum(x[2] for x in harm):+.2f}pp")
print(f"  ⇒ 净额 Σ={sum(x[2] for x in ben) + sum(x[2] for x in harm):+.2f}pp")
print(f"  ⇒ 受益/受损 笔数比 = {len(ben)}/{len(harm)} = {len(ben)/max(len(harm),1):.2f} "
      f"(毕业门要求一致性 ≥55%, 实测 {100*len(ben)/max(len(ben)+len(harm),1):.1f}%)")

print()
print(f"  --- 受益人群逐笔 (MFE≥{TH}% 且落袋<{TH}%) n={len(ben)} ---")
for m, land, d in sorted(ben, key=lambda z: -z[2])[:12]:
    print(f"    MFE={m:6.2f}%  落袋={land:6.2f}%  Δ={d:+.3f}pp "
          f"(= 0.5×({TH:.1f}−{land:.2f}) − {EXTRA_BP}bp)")
print()
print(f"  --- 受损人群最受伤 5 笔 ---")
for m, land, d in sorted(harm, key=lambda z: z[2])[:5]:
    print(f"    MFE={m:6.2f}%  落袋={land:6.2f}%  Δ={d:+.3f}pp")

# ── E19: ORCL 单标的捕获率分布 (痛点人群 vs 触发人群是否重叠) ──
orcl = c.execute(
    "SELECT mfe_pct, pnl_pct FROM trade_records "
    "WHERE symbol='US.ORCL' AND exit_time IS NOT NULL AND mfe_pct>0"
).fetchall()
cap = [(m, 100.0 * l * 100.0 / m) for m, l in orcl]
low = [(m, x) for m, x in cap if x < 60]
hi = [(m, x) for m, x in cap if x >= 95]
print()
print("=" * 88)
print("ORCL 单标的捕获率分布 (验证「痛点人群 ∩ 触发人群」是否重叠)")
print("=" * 88)
print(f"  n={len(cap)} low60={len(low)} low60_mfe_ge2={sum(1 for m,_ in low if m>=2.0)} "
      f"hi95={len(hi)} hi95_mfe_ge2={sum(1 for m,_ in hi if m>=2.0)}")
print(f"  ⇒ 捕获率<60% 的 {len(low)} 笔里只有 {sum(1 for m,_ in low if m>=2.0)} 笔摸到过 2% 线 "
      f"(其余 {len(low)-sum(1 for m,_ in low if m>=2.0)} 笔阶梯够不到)")
print(f"  ⇒ 捕获率≥95% 的 {len(hi)} 笔里有 {sum(1 for m,_ in hi if m>=2.0)} 笔 MFE≥2% "
      f"(= 阶梯会砍的正是这批已捕获良好的)")
