#!/usr/bin/env python3
"""富途三套餐逐笔重算 (决策一落地, 2026-08-04) — 用户提供真实费率表后对 era 151 笔重算.

只读. 复用 fee_structure_analysis.era_trades() 取 qty/价格/方向, 按三套餐公式逐笔算
round-trip 费用并对比实收口径 (fees_est, 旧套餐).

套餐公式 (用户提供, 2026-08-04):
  旧套餐   : 佣金 0.01/股 min1.99 max0.3%amt; 平台费 0; (现行 fee_model 口径)
  套餐一   : 佣金 0.0049/股 min0.99 max0.5%amt; 平台费 0.005/股 min1.00 max0.5%amt
  按股梯度 : 佣金 0.0049/股 min0.99 max0.5%amt; 平台费按当月累计股数阶梯 0.01→0.003/股
             min1.00 max0.5%amt (月度累计清零)
交收费/SEC/TAF/CAT 三套餐相同, 不受切换影响 → 只重算 佣金+平台费 两项差额.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from fee_structure_analysis import era_trades  # noqa: E402


def _leg_comm_old(qty: float, price: float) -> float:
    amt = qty * price
    return max(min(0.01 * qty, 0.003 * amt), 1.99)


def _leg_comm_plan1(qty: float, price: float) -> float:
    amt = qty * price
    return max(min(0.0049 * qty, 0.005 * amt), 0.99)


def _leg_plat_plan1(qty: float, price: float) -> float:
    amt = qty * price
    return max(min(0.005 * qty, 0.005 * amt), 1.00)


def _tier_rate(cum_shares: float) -> float:
    if cum_shares <= 500:
        return 0.01
    if cum_shares <= 1000:
        return 0.008
    if cum_shares <= 5000:
        return 0.007
    if cum_shares <= 10000:
        return 0.006
    if cum_shares <= 50000:
        return 0.0055
    if cum_shares <= 200000:
        return 0.005
    if cum_shares <= 500000:
        return 0.0045
    if cum_shares <= 1000000:
        return 0.004
    if cum_shares <= 5000000:
        return 0.0035
    return 0.003


def _leg_plat_tiered(qty: float, price: float, cum_shares: float) -> float:
    amt = qty * price
    return max(min(_tier_rate(cum_shares) * qty, 0.005 * amt), 1.00)


def main() -> None:
    trades = era_trades()
    print(f"era trades: {len(trades)}")

    tot = {"old": 0.0, "plan1": 0.0, "tiered": 0.0}
    cum = 0.0  # 月度累计股数; era 跨 06-23→08-03 含两次月初清零, 此处整段累计=对梯度档偏乐观
    per_trade = []
    for t in trades:
        qty = float(t["qty"])
        pe = float(t["entry"]); px = float(t["exit"] or pe)
        f_old = float(t["fees_est"] if t["fees_est"] is not None else t["fee_calc"])
        other = f_old - (_leg_comm_old(qty, pe) + _leg_comm_old(qty, px))
        f_p1 = sum(_leg_comm_plan1(qty, p) + _leg_plat_plan1(qty, p) for p in (pe, px)) + other
        f_t = 0.0
        for p in (pe, px):
            cum += qty
            f_t += _leg_comm_plan1(qty, p) + _leg_plat_tiered(qty, p, cum)
        f_t += other
        tot["old"] += f_old; tot["plan1"] += f_p1; tot["tiered"] += f_t
        per_trade.append({"symbol": t["symbol"], "qty": qty,
                          "old": round(f_old, 2), "plan1": round(f_p1, 2),
                          "tiered": round(f_t, 2)})

    print(f"\n累计股数 (era 全程粗算): {cum:,.0f}")
    print(f"\n费用合计 (era {len(trades)} 笔 round-trip):")
    print(f"  旧套餐 (实收口径 fees_est): {tot['old']:.2f}")
    print(f"  套餐一                    : {tot['plan1']:.2f}  Δ={tot['plan1']-tot['old']:+.2f}")
    print(f"  按股梯度                  : {tot['tiered']:.2f}  Δ={tot['tiered']-tot['old']:+.2f}")

    fl_old = sum(1 for t in trades
                 if _leg_comm_old(t["qty"], t["entry"]) <= 1.9901)
    fl_p1 = sum(1 for t in trades
                if _leg_comm_plan1(t["qty"], t["entry"]) <= 0.9901
                and _leg_plat_plan1(t["qty"], t["entry"]) <= 1.0001)
    print(f"\n地板命中 (入场腿): 旧 {fl_old}/{len(trades)}; 套餐一 {fl_p1}/{len(trades)}")

    deltas = [p["plan1"] - p["old"] for p in per_trade]
    better = sum(1 for d in deltas if d < -0.005)
    worse = sum(1 for d in deltas if d > 0.005)
    print(f"套餐一 vs 旧: 更便宜 {better} 笔 / 持平 {len(deltas)-better-worse} 笔 / 更贵 {worse} 笔")

    out = REPO / "data" / "fee_plan_compare_latest.json"
    out.write_text(json.dumps({
        "generated_at": "2026-08-04",
        "n_trades": len(trades),
        "cum_shares_era": cum,
        "totals": {k: round(v, 2) for k, v in tot.items()},
        "delta_plan1": round(tot["plan1"] - tot["old"], 2),
        "delta_tiered": round(tot["tiered"] - tot["old"], 2),
        "floor_hits": {"old": fl_old, "plan1": fl_p1, "n": len(trades)},
        "per_trade": per_trade,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n已落 {out}")


if __name__ == "__main__":
    main()
