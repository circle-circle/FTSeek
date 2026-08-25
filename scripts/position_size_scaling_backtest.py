#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""仓位规模 × 净期望 反事实 (2026-08-23, 纯算术, 只读零交易路径)。

═══ 被检验的问题 ═══
反向检验分解出: 当日毛方向 edge = +$1.76/笔, 而费用 = $4.54/笔 → 需 2.6x edge 才打平。
且实测 55% 的单触及 $1.99 最低佣金 (股数中位仅 17 股, 每股佣金 $0.01 → 应付 $0.17)。
问: 当前亏损里有多少纯粹是"仓位太小、付最低佣金"造成的? 放大到多少能跨过零?

═══ 方法 (无建模风险) ═══
不模拟任何退出政策 — 直接用【实际发生的】入场价/出场价/股数:
  gross_actual = sign × (exit_price − entry_price) × qty        (与实际一致)
  fees_actual  = estimate_fee(qty, ep, 入场腿) + estimate_fee(qty, xp, 出场腿)
  net = gross − fees   ← 已验证与 trade_records.pnl_net 吻合 (MAE $0.00)
放大 M 倍:
  gross_M = gross_actual × M       (线性, 无假设)
  fees_M  = 按真实费率表用 qty×M 重算 (自动含 最低佣金→按股 过渡 与 0.3%金额上限)
  net_M   = gross_M − fees_M

═══ 关键约束 (必须同时报, 否则结论是空中楼阁) ═══
risk_base_capital = $82,000 × max_position_pct = 6% → 单笔名义额硬上限 $4,920。
当前名义额中位 $3,853 = 上限的 78% → 放大倍数在风控框架内只有 ~1.28x。

═══ 诚实披露 ═══
① 本回测的上界 = 总毛额 (M→∞ 时 net → gross_total)。若 gross_total ≈ 0, 则放大仓位
   只能"止血"不能"盈利"。该上界必须与结论并列呈现。
② 放大仓位放大方差与单笔敞口, 会撞 max_position_pct / 日亏损帽 / 水坝单轮3笔限。
   8/14 实测"十笔LONG挤87秒齐射, 水坝窗均笔亏损5.5x" — 放大必须同时收紧并发。
③ 假设放大后成交价不变 (无冲击成本)。名义额 $4k 级别在流动性门 ADV 0.5% 内, 但
   放大 10x 后该假设失效 → 大倍数行仅作算术参考, 不可解读为可实现。
④ 只做观测, 零生产路径改动。

输出: data/position_size_scaling_report.json + 控制台
"""
import json
import sqlite3
import statistics
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
DB = REPO / "data" / "ftseek_us.db"
OUT = REPO / "data" / "position_size_scaling_report.json"
SINCE = "2026-07-01"
MULTS = [0.5, 1, 1.28, 2, 3, 5, 10, 20]
RISK_BASE_CAPITAL = 82000.0
MAX_POSITION_PCT = 0.06


def main():
    from app.execution.fee_model import estimate_fee

    cc = sqlite3.connect(str(DB), timeout=10)
    rows = cc.execute(
        """SELECT symbol, direction, trade_date, exit_time, entry_price, exit_price,
                  quantity, pnl_net FROM trade_records
           WHERE source_type='PLAYBOOK_SIM' AND exit_time IS NOT NULL
             AND trade_date >= ? AND pnl_net IS NOT NULL
             AND entry_price > 0 AND exit_price > 0 AND quantity > 0""", (SINCE,)).fetchall()
    cc.close()

    trades = []
    for sym, d, td, xt, ep, xp, q, net in rows:
        short = str(d).upper() == "SHORT"
        ep, xp, q = float(ep), float(xp), float(q)
        sign = -1.0 if short else 1.0
        trades.append({"sym": sym, "short": short, "td": td, "xdate": str(xt)[:10],
                       "ep": ep, "xp": xp, "qty": q, "actual_net": float(net),
                       "gross": sign * (xp - ep) * q})
    n = len(trades)
    gross_total = sum(t["gross"] for t in trades)

    def fees_at(t, m):
        eside, xside = (("SELL", "BUY") if t["short"] else ("BUY", "SELL"))
        q = max(1.0, round(t["qty"] * m))
        try:
            return (estimate_fee(q, t["ep"], eside, t["td"])["total"]
                    + estimate_fee(q, t["xp"], xside, t["xdate"])["total"])
        except Exception:
            return 3.98

    # 自校验
    base_fees = sum(fees_at(t, 1) for t in trades)
    recon = gross_total - base_fees
    actual = sum(t["actual_net"] for t in trades)
    print(f"样本 n={n}")
    print(f"自校验: 重建净额 ${recon:+.1f} vs 实际 pnl_net ${actual:+.1f} (差 ${recon-actual:+.1f})")
    print(f"总毛额 ${gross_total:+.1f}  总费用 ${base_fees:.1f}  → 费用是毛额的 "
          f"{abs(base_fees/gross_total) if gross_total else float('inf'):.1f} 倍")

    notional = [t["ep"] * t["qty"] for t in trades]
    med_notional = statistics.median(notional)
    cap = RISK_BASE_CAPITAL * MAX_POSITION_PCT
    print(f"\n名义额中位 ${med_notional:,.0f} | 风控单笔上限 ${cap:,.0f} "
          f"(= ${RISK_BASE_CAPITAL:,.0f} × {MAX_POSITION_PCT:.0%}) "
          f"→ 框架内最大倍数 {cap/med_notional:.2f}x")

    grid = []
    for m in MULTS:
        g = gross_total * m
        f = sum(fees_at(t, m) for t in trades)
        med_n = med_notional * m
        n_over_cap = sum(1 for x in notional if x * m > cap)
        grid.append({
            "mult": m, "gross": round(g, 1), "fees": round(f, 1), "net": round(g - f, 1),
            "net_per_trade": round((g - f) / n, 2),
            "median_notional": round(med_n, 0),
            "fee_bps_round_trip": round(f / n / med_n * 10000, 2),
            "gross_bps_round_trip": round(g / n / med_n * 10000, 2),
            "n_trades_over_risk_cap": n_over_cap,
            "within_risk_framework": bool(n_over_cap == 0),
        })

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "method": "纯算术: 实际入出场价/股数, 毛额线性缩放, 费用按真实费率表重算",
        "n_trades": n,
        "self_check": {"reconstructed_net": round(recon, 1),
                       "actual_pnl_net_sum": round(actual, 1),
                       "diff": round(recon - actual, 1)},
        "gross_total": round(gross_total, 1),
        "asymptote_note": "M→∞ 时 net → gross_total, 这是本杠杆的硬上界",
        "risk_constraint": {"risk_base_capital": RISK_BASE_CAPITAL,
                            "max_position_pct": MAX_POSITION_PCT,
                            "max_notional_per_trade": cap,
                            "current_median_notional": round(med_notional, 0),
                            "max_mult_within_framework": round(cap / med_notional, 2)},
        "disclosures": [
            "上界=总毛额; gross_total≈0 则放大只能止血不能盈利",
            "放大仓位放大方差, 撞 max_position_pct/日亏帽/水坝3笔限; 8/14水坝事故均笔亏损5.5x",
            "假设无冲击成本; 大倍数行仅算术参考不可解读为可实现",
            "零生产路径改动",
        ],
        "grid": grid,
    }
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n{'倍数':>6} {'名义额中位':>11} {'总毛':>9} {'总费':>8} {'净额':>9} "
          f"{'净/笔':>8} {'毛bps':>7} {'费bps':>7} {'超风控帽笔数':>11}")
    for r in grid:
        flag = "" if r["within_risk_framework"] else "  ⚠️超帽"
        print(f"{r['mult']:>6.2f} {r['median_notional']:>11,.0f} {r['gross']:>+9.0f} "
              f"{r['fees']:>8.0f} {r['net']:>+9.0f} {r['net_per_trade']:>+8.2f} "
              f"{r['gross_bps_round_trip']:>7.1f} {r['fee_bps_round_trip']:>7.1f} "
              f"{r['n_trades_over_risk_cap']:>11}{flag}")
    print(f"\n硬上界 (M→∞): net → 总毛额 ${gross_total:+.1f} "
          f"(= ${gross_total/n:+.2f}/笔)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
