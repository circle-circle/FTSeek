#!/usr/bin/env python3
"""F-S4 费用结构尺子 + 杠杆评估 (DEB-20260804-scan-subscribe-entry-ruler §六, 2026-08-04).

只读: trade_records era 窗 + fee_model 纯函数, 不写任何 live 文件/配置.
结果落 data/fee_structure_latest.json.

背景: era 净亏 -1040.3 中费用占 65% ($4.48/笔 = 名义 14.6bps round-trip),
min$4.01 地板 = $1.99 最低佣金×2 → 小名义仓位的尺寸伪影. 本尺子回答:
  ①费用% vs 名义尺寸曲线 (地板在哪里松开)
  ②era 实测费用分布
  ③三杠杆 paper 评估: A 提名义 / B 降笔数 (剔地板区小单) / C 费率档 (假想议价)

保真度边界 (如实报告): SIM 成交与滑点 bps 假设不随尺寸变; 无市场冲击;
名义放大等比放大单笔风险敞口 — 是否可执行归 sizing 治理, 本脚本只量费用杠杆.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from app.execution import fee_model  # noqa: E402

DB = REPO / "data" / "ftseek_us.db"
OUT = REPO / "data" / "fee_structure_latest.json"
ERA_SINCE = "2026-07-14"


def _rt_fee(qty: float, entry: float, exit_: float, direction: str, day: str) -> float:
    return fee_model.estimate_round_trip(qty, entry, exit_, direction, day, day)["total"]


def fee_curve() -> list[dict]:
    """费用 bps vs 名义尺寸, 分代表价格. qty 取整 (实盘手=1股)。"""
    prices = [5, 15, 30, 60, 150, 400]
    notionals = [1000, 1500, 2000, 2500, 3000, 4000, 5000, 6000, 8000,
                 10000, 15000, 20000, 30000]
    rows = []
    for p in prices:
        for n in notionals:
            qty = max(1, round(n / p))
            amt = qty * p
            fee = _rt_fee(qty, p, p, "LONG", "2026-08-01")
            rows.append({"price": p, "target_notional": n, "qty": qty,
                         "notional": round(amt, 2), "fee": round(fee, 2),
                         "bps": round(fee / amt * 1e4, 1),
                         "floor_bound": qty < 199})
    return rows


def era_trades() -> list[dict]:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT trade_date, symbol, direction, quantity, entry_price, exit_price, "
        "realized_pnl, pnl_net, fees_est FROM trade_records "
        "WHERE trade_date>=? AND exit_price>0", (ERA_SINCE,)).fetchall()
    con.close()
    out = []
    for r in rows:
        gross = r["realized_pnl"] or 0.0
        qty = r["quantity"] or 0
        fee_est = r["fees_est"] if r["fees_est"] is not None else None
        fee_calc = _rt_fee(qty, r["entry_price"], r["exit_price"], r["direction"],
                           r["trade_date"])
        out.append({"date": r["trade_date"], "symbol": r["symbol"],
                    "direction": r["direction"], "qty": qty,
                    "entry": r["entry_price"], "exit": r["exit_price"],
                    "notional": qty * r["entry_price"], "gross": gross,
                    "fees_est": fee_est, "fee_calc": fee_calc,
                    "net": gross - (fee_est if fee_est is not None else fee_calc)})
    return out


def _sum(xs): return round(sum(xs), 2)


def lever_a_scale(trades: list[dict]) -> list[dict]:
    """名义 ×k: gross 线性, 费用按 fee_model 重算 (地板松开 → bps 下降)。"""
    out = []
    base_net = _sum(t["net"] for t in trades)
    for k in [1, 1.5, 2, 3, 5]:
        net = gross = fee = 0.0
        for t in trades:
            q2 = max(1, round(t["qty"] * k))
            f2 = _rt_fee(q2, t["entry"], t["exit"], t["direction"], t["date"])
            gross += t["gross"] * k
            fee += f2
            net += t["gross"] * k - f2
        out.append({"k": k, "gross": _sum([gross]), "fee": _sum([fee]),
                    "net": _sum([net]), "delta_vs_base": round(net - base_net, 2),
                    "per_trade": round(net / len(trades), 2)})
    return out


def lever_a_fixed_notional(trades: list[dict]) -> list[dict]:
    """每笔固定名义 (qty=round(N/entry)): 费用地板稀释到接近按比例档。"""
    out = []
    for n in [6000, 10000, 15000]:
        net = gross = fee = 0.0
        for t in trades:
            q2 = max(1, round(n / t["entry"]))
            f2 = _rt_fee(q2, t["entry"], t["exit"], t["direction"], t["date"])
            g2 = t["gross"] * (q2 / t["qty"]) if t["qty"] else 0.0
            gross += g2
            fee += f2
            net += g2 - f2
        out.append({"fixed_notional": n, "gross": _sum([gross]), "fee": _sum([fee]),
                    "net": _sum([net]), "per_trade": round(net / len(trades), 2)})
    return out


def lever_b_drop_small(trades: list[dict]) -> list[dict]:
    """剔名义 <T 的地板区小单: 报剩余净 + 被剔子集盈亏 (小单是否带信号)。"""
    out = []
    for t_thresh in [1500, 2000, 2500, 3000, 4000]:
        keep = [t for t in trades if t["notional"] >= t_thresh]
        drop = [t for t in trades if t["notional"] < t_thresh]
        out.append({"min_notional": t_thresh, "kept_n": len(keep),
                    "kept_net": _sum(t["net"] for t in keep),
                    "dropped_n": len(drop),
                    "dropped_gross": _sum(t["gross"] for t in drop),
                    "dropped_net": _sum(t["net"] for t in drop)})
    return out


def lever_c_fee_tier(trades: list[dict]) -> dict:
    """假想费率档 (议价/换券商): 佣金 $0.005/股 min$1.00 — 纯假设, 标注勿当承诺。"""
    base_net = _sum(t["net"] for t in trades)
    orig_cache = fee_model._schedule_cache
    try:
        sched = dict(fee_model.get_schedule())
        sched["commission_per_share"] = 0.005
        sched["commission_min_per_order"] = 1.00
        fee_model._schedule_cache = sched
        fee = sum(_rt_fee(t["qty"], t["entry"], t["exit"], t["direction"], t["date"])
                  for t in trades)
        gross = _sum(t["gross"] for t in trades)
        return {"assumption": "commission $0.005/股 min$1.00 (假想, 非券商承诺)",
                "fee": _sum([fee]), "net": round(gross - fee, 2),
                "delta_vs_base": round(gross - fee - base_net, 2)}
    finally:
        fee_model._schedule_cache = orig_cache


def main() -> None:
    print("=== F-S4 费用结构尺子 ===")
    curve = fee_curve()
    print("\n-- ①费用 bps vs 名义 (floor=qty<199 即 $1.99×2 地板) --")
    hdr = "price\\notl " + " ".join(f"{n:>7}" for n in
                                  [1000, 2000, 3000, 5000, 8000, 10000, 20000, 30000])
    print(hdr)
    pick = [1000, 2000, 3000, 5000, 8000, 10000, 20000, 30000]
    for p in [5, 15, 30, 60, 150, 400]:
        cells = []
        for n in pick:
            r = next(x for x in curve if x["price"] == p and x["target_notional"] == n)
            cells.append(f"{r['bps']:>6.1f}{'*' if r['floor_bound'] else ' '}")
        print(f"${p:>5}    " + " ".join(cells))
    print("(* = 佣金地板 binding)")

    trades = era_trades()
    n = len(trades)
    fees = sorted(t["fee_calc"] for t in trades)
    bpss = sorted(t["fee_calc"] / t["notional"] * 1e4 for t in trades if t["notional"] > 0)
    gross_sum = _sum(t["gross"] for t in trades)
    fee_est_sum = _sum(t["fees_est"] or 0 for t in trades)
    fee_calc_sum = _sum(t["fee_calc"] for t in trades)
    net_sum = _sum(t["net"] for t in trades)
    print(f"\n-- ②era 实测 (n={n}, {ERA_SINCE}+) --")
    print(f"gross={gross_sum:+.2f}  fees_est={fee_est_sum:.2f}  fee_model重算={fee_calc_sum:.2f}  "
          f"net={net_sum:+.2f}  (est vs 模型差 {fee_est_sum - fee_calc_sum:+.2f})")
    print(f"费用/笔: avg={sum(fees)/n:.2f} p50={fees[n//2]:.2f} "
          f"p90={fees[int(n*0.9)]:.2f} max={fees[-1]:.2f}")
    print(f"费用bps: avg={sum(bpss)/n:.1f} p50={bpss[n//2]:.1f} "
          f"p90={bpss[int(n*0.9)]:.1f} max={bpss[-1]:.1f}")
    floor_n = sum(1 for t in trades if t["qty"] < 199)
    print(f"地板区 (qty<199): {floor_n}/{n} = {floor_n/n*100:.0f}%")

    la = lever_a_scale(trades)
    print("\n-- ③A 名义×k (gross线性/费用重算) --")
    for r in la:
        print(f"  k={r['k']:>3}: gross={r['gross']:+9.2f} fee={r['fee']:7.2f} "
              f"net={r['net']:+9.2f} Δ={r['delta_vs_base']:+8.2f} 每笔={r['per_trade']:+8.2f}")
    la2 = lever_a_fixed_notional(trades)
    print("-- ③A2 固定名义 --")
    for r in la2:
        print(f"  ${r['fixed_notional']:>6}/笔: gross={r['gross']:+9.2f} fee={r['fee']:7.2f} "
              f"net={r['net']:+9.2f} 每笔={r['per_trade']:+.2f}")
    # 剔暴跌日子集: 毛期望为正时尺寸杠杆才是免费的 (③A 的关键依赖)
    xc = [t for t in trades if t["date"] not in ("2026-07-21", "2026-07-22", "2026-07-23")]
    la_x = lever_a_scale(xc)
    print(f"-- ③A-ex 剔暴跌日 (n={len(xc)}, 毛为正子集的尺寸杠杆) --")
    for r in la_x:
        print(f"  k={r['k']:>3}: gross={r['gross']:+9.2f} fee={r['fee']:7.2f} "
              f"net={r['net']:+9.2f} Δ={r['delta_vs_base']:+8.2f} 每笔={r['per_trade']:+.2f}")
    lb = lever_b_drop_small(trades)
    print("-- ③B 剔地板区小单 --")
    for r in lb:
        print(f"  名义≥${r['min_notional']:>5}: 留{r['kept_n']:>3}笔 net={r['kept_net']:+9.2f} | "
              f"剔{r['dropped_n']:>3}笔 (毛{r['dropped_gross']:+8.2f} 净{r['dropped_net']:+8.2f})")
    lc = lever_c_fee_tier(trades)
    print(f"-- ③C 假想费率档 -- {lc['assumption']}")
    print(f"  fee={lc['fee']:.2f} net={lc['net']:+.2f} Δ={lc['delta_vs_base']:+.2f}")

    result = {
        "_meta": {"generated_by": "scripts/fee_structure_analysis.py",
                  "debate": "DEB-20260804-scan-subscribe-entry-ruler §六 F-S4",
                  "era_since": ERA_SINCE, "n_trades": n,
                  "caveats": ["SIM成交/滑点bps不随尺寸变", "无市场冲击",
                              "名义放大等比放大单笔风险敞口, 可执行性归sizing治理",
                              "费率档为假想非承诺"]},
        "fee_curve": curve,
        "era_actuals": {"gross": gross_sum, "fees_est": fee_est_sum,
                        "fee_model_recalc": fee_calc_sum, "net": net_sum,
                        "fee_per_trade_avg": round(sum(fees) / n, 2),
                        "fee_bps_avg": round(sum(bpss) / n, 1),
                        "floor_bound_share": round(floor_n / n, 3)},
        "lever_a_scale": la, "lever_a_fixed_notional": la2,
        "lever_a_scale_ex_crash": la_x, "ex_crash_n": len(xc),
        "lever_b_drop_small": lb, "lever_c_fee_tier": lc,
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=1))
    print(f"\n已落 {OUT}")


if __name__ == "__main__":
    main()
