#!/usr/bin/env python
"""DEB 立案前绑定预检 — 2%/3%/4% 分批止盈阶梯的死参数三重检验 (①).

skill exit-parameter-validation 核心流程第 0 步: 待调参数必须先过死参数检验 —
「0 笔进过参数区间 = 从不绑定」⇒ 该参数线整体否决, 不进模拟/OOS/DEB。

本脚本答三问:
  Q1 触发人群: 有多少笔 MFE 真的摸到过 2% / 3% / 4%? (阶梯的触发上限)
  Q2 这些笔的**现状结局**如何? (若现状已经落袋良好 ⇒ 阶梯无残存效应)
  Q3 qty 成本: 分批多一腿的 $1.99 min commission 占名义多少 bp?
     (用户明确问「一方面费用问题一方面是qty数量问题」)

量纲铁律 (DEB-20261007 §5): mfe_pct/mae_pct = 百分点; pnl_pct = 小数比例。
只读 (mode=ro), 零写入。
用法: cd /opt/ftseek/FTSeek-US && venv/bin/python scripts/deb20261008_scaleout_binding.py
"""
from pathlib import Path as _P
_ROOT = str(_P(__file__).resolve().parents[1])

import collections
import json
import sqlite3
import statistics as st

c = sqlite3.connect(f"file:{_ROOT}/data/ftseek_us.db?mode=ro", uri=True)
c.row_factory = sqlite3.Row

# 费用模型 (生产 SSOT, 不自写第二套)
import sys
sys.path.insert(0, _ROOT)
from app.execution.fee_model import get_schedule  # noqa: E402

SCHED = get_schedule()
MIN_COMM = SCHED["commission_min_per_order"]
PER_SHARE = SCHED["commission_per_share"]
SETTLE_PS = SCHED["settlement_per_share"]

rows = c.execute(
    "SELECT id, symbol, trade_date, direction, entry_price, exit_price, quantity, "
    "       pnl_net, pnl_pct, mfe_pct, mae_pct, fees_est, exit_reason, holding_hours "
    "FROM trade_records WHERE exit_time IS NOT NULL"
).fetchall()

recs = []
for r in rows:
    if r["mfe_pct"] is None or r["entry_price"] in (None, 0):
        continue
    qty = r["quantity"] or 0
    px = r["entry_price"]
    notional = float(qty) * float(px) if qty and px else 0.0
    recs.append({
        "id": r["id"], "sym": r["symbol"], "d": r["trade_date"], "dir": r["direction"],
        "mfe": float(r["mfe_pct"]),                     # 百分点
        "landed": float(r["pnl_pct"]) * 100.0 if r["pnl_pct"] is not None else None,  # → 百分点
        "net": float(r["pnl_net"]) if r["pnl_net"] is not None else None,
        "fees": float(r["fees_est"]) if r["fees_est"] is not None else None,
        "er": str(r["exit_reason"] or "NULL"), "qty": qty, "notional": notional,
    })

print(f"样本 n={len(recs)}  (exit_time 非空 + mfe_pct 非空)")
mfes = sorted(x["mfe"] for x in recs)


def pct(v, p):
    return v[min(int(len(v) * p / 100), len(v) - 1)] if v else 0.0


print(f"MFE 分布(百分点): 中位={pct(mfes,50):.2f}  p75={pct(mfes,75):.2f}  "
      f"p90={pct(mfes,90):.2f}  p95={pct(mfes,95):.2f}  max={mfes[-1]:.2f}")

print("\n" + "=" * 92)
print("Q1 绑定预检: 阶梯触发人群 (MFE 摸到过阈值线的笔数与占比)")
print("=" * 92)
print(f"  {'阈值':>8s} {'触发笔数':>8s} {'占比':>7s}  {'该批现状净额':>14s} {'现状中位落袋':>12s} {'现状WR':>7s}")
for th in (1.0, 1.5, 2.0, 2.5, 3.0, 4.0):
    grp = [x for x in recs if x["mfe"] >= th]
    if not grp:
        print(f"  {th:7.1f}% {0:8d} {0:6.1f}%")
        continue
    nets = [x["net"] for x in grp if x["net"] is not None]
    lands = [x["landed"] for x in grp if x["landed"] is not None]
    wr = 100 * sum(1 for v in lands if v > 0) / len(lands) if lands else 0.0
    print(f"  {th:7.1f}% {len(grp):8d} {100*len(grp)/len(recs):6.1f}%  "
          f"${sum(nets):13,.2f} {st.median(lands):11.2f}% {wr:6.1f}%")

print("\n" + "=" * 92)
print("Q2 残存效应: 摸到过 2%/3%/4% 的笔, 现状是否已经落袋良好?")
print("   (若现状落袋已接近 MFE ⇒ 阶梯无残存空间; 若落袋远小于 MFE ⇒ 有回吐可治)")
print("=" * 92)
print(f"  {'阈值':>8s} {'n':>5s} {'MFE中位':>8s} {'落袋中位':>9s} {'捕获率':>8s} {'回吐中位':>9s} "
      f"{'落袋<0笔数':>10s}")
for th in (2.0, 3.0, 4.0):
    grp = [x for x in recs if x["mfe"] >= th and x["landed"] is not None]
    if not grp:
        continue
    m = st.median([x["mfe"] for x in grp])
    l = st.median([x["landed"] for x in grp])
    cap = 100 * l / m if m else 0.0
    neg = sum(1 for x in grp if x["landed"] < 0)
    print(f"  {th:7.1f}% {len(grp):5d} {m:7.2f}% {l:8.2f}% {cap:7.1f}% {m-l:8.2f}% {neg:10d}")

print("\n  --- 摸到 2% 但最终落袋为负的笔 (阶梯的真正目标人群) ---")
tgt = [x for x in recs if x["mfe"] >= 2.0 and x["landed"] is not None and x["landed"] < 0]
print(f"  n={len(tgt)}  净额=${sum(x['net'] or 0 for x in tgt):,.2f}")
er = collections.Counter(x["er"] for x in tgt)
print(f"  出场原因分布: {dict(er.most_common(8))}")
print(f"  MFE 中位={st.median([x['mfe'] for x in tgt]):.2f}%  "
      f"落袋中位={st.median([x['landed'] for x in tgt]):.2f}%")
print("  ⇒ 若这批的 MFE 中位 ≥2% 而落袋为负 = 回吐确凿; 但须核出场原因是否已是 SL_TRAIL_LOCK/BE")
print("     (若已是锁利出场 ⇒ 阶梯与现役机制重叠, 残存效应趋零 — 同 MFE 停滞案教训)")

print("\n" + "=" * 92)
print("Q3 qty 成本: 分批多一腿的费用 (用户问的「费用问题 + qty 数量问题」)")
print(f"   费率表实值: 佣金 ${PER_SHARE}/股, min ${MIN_COMM}/单, 交收费 ${SETTLE_PS}/股")
print("=" * 92)
nom = [x["notional"] for x in recs if x["notional"] > 0]
qtys = [x["qty"] for x in recs if x["qty"] > 0]
print(f"  名义额: 中位=${st.median(nom):,.0f}  p25=${pct(sorted(nom),25):,.0f}  "
      f"p75=${pct(sorted(nom),75):,.0f}")
print(f"  qty   : 中位={st.median(qtys):.0f}  p25={pct(sorted(qtys),25):.0f}  "
      f"p75={pct(sorted(qtys),75):.0f}")
small = [q for q in qtys if q <= 5]
odd = [q for q in qtys if q % 2 == 1]
print(f"  qty 小尾: ≤5股 {len(small)} 笔 ({100*len(small)/len(qtys):.1f}%) | "
      f"奇数 {len(odd)} 笔 ({100*len(odd)/len(qtys):.1f}%, 对半分产生碎股)")

# min commission 绑定率: $0.01*qty < $1.99 ⇒ qty < 199 ⇒ min 绑定
bind = [q for q in qtys if PER_SHARE * q < MIN_COMM]
print(f"\n  ① 现状单腿: min commission 绑定率 = {100*len(bind)/len(qtys):.1f}% "
      f"(qty<{MIN_COMM/PER_SHARE:.0f} 股时 $1.99 地板生效)")

print(f"\n  ② 分批一半后 (qty/2) 的 min 绑定率与增量费用:")
print(f"     {'名义档':>12s} {'n':>5s} {'单腿费':>9s} {'两腿费':>9s} {'增量':>8s} {'增量bp':>8s}")
buckets = [(0, 3000, "<$3k"), (3000, 6000, "$3-6k"), (6000, 99999999, ">$6k")]
for lo, hi, lab in buckets:
    grp = [x for x in recs if lo <= x["notional"] < hi and x["qty"] > 0]
    if len(grp) < 5:
        continue
    inc, bps = [], []
    for x in grp:
        q = x["qty"]
        one = max(PER_SHARE * q, MIN_COMM) + SETTLE_PS * q
        q1, q2 = q // 2, q - q // 2
        two = (max(PER_SHARE * q1, MIN_COMM) + SETTLE_PS * q1
               + max(PER_SHARE * q2, MIN_COMM) + SETTLE_PS * q2)
        inc.append(two - one)
        if x["notional"] > 0:
            bps.append(10000 * (two - one) / x["notional"])
    print(f"     {lab:>12s} {len(grp):5d} ${st.median([max(PER_SHARE*x['qty'],MIN_COMM)+SETTLE_PS*x['qty'] for x in grp]):8.2f} "
          f"${st.median([max(PER_SHARE*(x['qty']//2),MIN_COMM)+SETTLE_PS*(x['qty']//2)+max(PER_SHARE*(x['qty']-x['qty']//2),MIN_COMM)+SETTLE_PS*(x['qty']-x['qty']//2) for x in grp]):8.2f} "
          f"${st.median(inc):7.2f} {st.median(bps):7.1f}")

print(f"\n  ③ 全样本分批增量费用: 中位=${st.median([max(PER_SHARE*(x['qty']//2),MIN_COMM)+SETTLE_PS*(x['qty']//2)+max(PER_SHARE*(x['qty']-x['qty']//2),MIN_COMM)+SETTLE_PS*(x['qty']-x['qty']//2)-max(PER_SHARE*x['qty'],MIN_COMM)-SETTLE_PS*x['qty'] for x in recs if x['qty']>0]):.2f}/笔")
allbp = [10000 * ((max(PER_SHARE*(x['qty']//2),MIN_COMM)+SETTLE_PS*(x['qty']//2)+max(PER_SHARE*(x['qty']-x['qty']//2),MIN_COMM)+SETTLE_PS*(x['qty']-x['qty']//2)) - (max(PER_SHARE*x['qty'],MIN_COMM)+SETTLE_PS*x['qty'])) / x['notional']
         for x in recs if x['qty'] > 0 and x['notional'] > 0]
print(f"     增量 bp 中位={st.median(allbp):.1f}bp  均值={st.mean(allbp):.1f}bp  p90={pct(sorted(allbp),90):.1f}bp")
cur = [10000 * x["fees"] / x["notional"] for x in recs if x["fees"] and x["notional"] > 0]
print(f"     对照: 现状单边费用中位={st.median(cur):.1f}bp ⇒ 分批增量相当于现状费用的 "
      f"{100*st.median(allbp)/max(st.median(cur),1e-9):.0f}%")

print("\n" + "=" * 92)
print("结论行 (供 DEB 引用)")
print("=" * 92)
g2 = [x for x in recs if x["mfe"] >= 2.0]
g3 = [x for x in recs if x["mfe"] >= 3.0]
g4 = [x for x in recs if x["mfe"] >= 4.0]
print(f"  MFE≥2% 触发人群 n={len(g2)} ({100*len(g2)/len(recs):.1f}%) | "
      f"≥3% n={len(g3)} ({100*len(g3)/len(recs):.1f}%) | ≥4% n={len(g4)} ({100*len(g4)/len(recs):.1f}%)")
print(f"  分批增量费用中位={st.median(allbp):.1f}bp (现状单边 {st.median(cur):.1f}bp)")
print(f"  min commission 绑定率: 单腿 {100*len(bind)/len(qtys):.1f}%")
