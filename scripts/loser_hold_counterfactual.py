#!/usr/bin/env python3
"""亏损单持有翻盘反事实 (2026-08-06, 用户需求①② 立案测量, 只读).

问题 (用户原话): "对于亏损股 除非达到最大亏损, 否则保留, 可以隔夜, 保留翻盘机会".
测量: era 已成交单中 **exit 时点亏损者** (exit_ret<0), 若不退出继续持有:
  · T+0 收盘 (当日拿到收盘) — 由 fwd_return_intraday 折算
  · T+1 收盘 (隔夜一日)      — fwd_return_t1
  · T+3 收盘 (隔三日)        — fwd_return_t3
每视界报: 增量均值 (sign×(P_h − exit)/exit, 毛) + CI95 + 双侧 null-pooled bootstrap p
  + **翻盘率** (该视界价格使该笔由亏转盈的比例) + 增量$ (notional=exit×position_size).
对称: 赢家 (exit_ret>0) 同表 — 供主张①"落袋为安"的机会成本对照.

口径与统计完全沿用 overnight_continuation_report.py (DEB-20260805 门D 同源):
  exit_ret = sign×(exit−entry)/entry; 目标价收益由 fwd 回填折算 incr=(1+fwd)/(1+exit_ret)−1;
  费用中性 (不多平仓笔数); SHORT borrow <1bp/日不扣 (大盘股, 判读留意).
  null-pooled bootstrap B=10,000 双侧, 种子同源. 见数后不改判据.

输出: 控制台表 + data/loser_hold_counterfactual_latest.json
用法: ./venv/bin/python scripts/loser_hold_counterfactual.py [days=60]
"""
import json
import random
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "ftseek_us.db"
OUT = ROOT / "data" / "loser_hold_counterfactual_latest.json"
DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 60
B = 10_000
MIN_N = 5


def null_pooled_bootstrap(vals):
    n = len(vals)
    mean = sum(vals) / n
    centered = [v - mean for v in vals]
    rng = random.Random(20260805)
    boot = []
    for _ in range(B):
        s = sum(centered[rng.randrange(n)] for _ in range(n)) / n
        boot.append(s + mean)
    boot.sort()
    ci_lo, ci_hi = boot[int(0.025 * B)], boot[int(0.975 * B)]
    rng2 = random.Random(20260805 + 1)
    null_means = sorted(
        sum(centered[rng2.randrange(n)] for _ in range(n)) / n for _ in range(B)
    )
    p = 2 * sum(1 for m in null_means if abs(m) >= abs(mean)) / B
    return mean, ci_lo, ci_hi, min(p, 1.0)


def main():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT date, symbol, direction, entry_price, exit_price, exit_reason,
                  fwd_return_intraday, fwd_return_t1, fwd_return_t3,
                  pnl, position_size
           FROM playbook_instructions
           WHERE triggered=1 AND executed=1 AND exit_price IS NOT NULL AND exit_price>0
             AND entry_price>0 AND fwd_return_t1 IS NOT NULL
             AND date >= date('now', ?)
        """,
        (f"-{DAYS} days",),
    ).fetchall()

    recs = []
    for r in rows:
        sign = 1.0 if r["direction"] == "BUY" else -1.0
        exit_ret = sign * (r["exit_price"] - r["entry_price"]) / r["entry_price"]
        if exit_ret <= -0.99:
            continue
        notional = r["exit_price"] * (r["position_size"] or 0)
        rec = {"date": r["date"], "symbol": r["symbol"], "dir": r["direction"],
               "exit_reason": r["exit_reason"], "exit_ret": exit_ret,
               "pnl": r["pnl"] or 0.0, "notional": notional}
        for hz, col in (("t0_close", "fwd_return_intraday"), ("t1", "fwd_return_t1"),
                        ("t3", "fwd_return_t3")):
            fwd = r[col]
            rec[hz] = ((1 + fwd) / (1 + exit_ret) - 1) if fwd is not None else None
        recs.append(rec)

    print(f"样本: {len(recs)} 笔 (近{DAYS}日, 已成交且有T+1回填)")
    losers = [x for x in recs if x["exit_ret"] < 0]
    winners = [x for x in recs if x["exit_ret"] > 0]
    print(f"输家 {len(losers)} / 赢家 {len(winners)}\n")

    def cell(name, sub, hz):
        vals = [x[hz] for x in sub if x[hz] is not None]
        if len(vals) < MIN_N:
            print(f"{name:<28} n={len(vals)} (<{MIN_N}, 不判)")
            return None
        mean, lo, hi, p = null_pooled_bootstrap(vals)
        # 翻盘率: 该视界使单笔由亏转盈 (exit_ret + incr 复利后 >0 → (1+exit_ret)(1+incr)>1)
        flip = sum(1 for x in sub if x[hz] is not None
                   and (1 + x["exit_ret"]) * (1 + x[hz]) > 1) / len(vals)
        usd = sum((x[hz] or 0) * x["notional"] for x in sub if x[hz] is not None)
        print(f"{name:<28} n={len(vals):>3}  增量均值={mean*100:+6.2f}%  "
              f"CI95[{lo*100:+.2f},{hi*100:+.2f}]  p={p:.4f}  翻盘率={flip:.0%}  "
              f"增量$=${usd:+.0f}")
        return {"n": len(vals), "incr_mean": round(mean, 5),
                "ci95": [round(lo, 5), round(hi, 5)], "p": round(p, 4),
                "flip_rate": round(flip, 3), "incr_usd": round(usd, 2)}

    out = {"generated_by": "scripts/loser_hold_counterfactual.py",
           "window_days": DAYS, "n_total": len(recs),
           "n_losers": len(losers), "n_winners": len(winners), "cells": {}}
    for hz, lbl in (("t0_close", "持有到当日收盘"), ("t1", "隔夜到T+1收盘"),
                    ("t3", "持有到T+3收盘")):
        print(f"── {hz} ({lbl}) ──")
        for name, sub in (("输家·全体", losers),
                          ("输家·BUY", [x for x in losers if x["dir"] == "BUY"]),
                          ("输家·SHORT", [x for x in losers if x["dir"] == "SELL_SHORT"]),
                          ("赢家·全体", winners)):
            r = cell(name, sub, hz)
            if r:
                out["cells"][f"{hz}|{name}"] = r
        print()

    # 大亏解剖: 亏损额 top8 逐笔
    worst = sorted(losers, key=lambda x: x["pnl"])[:8]
    print("── 大亏解剖 (毛亏 top8) ──")
    detail = []
    for x in worst:
        d = {"date": x["date"], "symbol": x["symbol"], "dir": x["dir"],
             "exit_reason": x["exit_reason"], "exit_ret_pct": round(x["exit_ret"] * 100, 2),
             "pnl_gross": round(x["pnl"], 2),
             "t0_incr_pct": round(x["t0_close"] * 100, 2) if x["t0_close"] is not None else None,
             "t1_incr_pct": round(x["t1"] * 100, 2) if x["t1"] is not None else None,
             "t3_incr_pct": round(x["t3"] * 100, 2) if x["t3"] is not None else None}
        detail.append(d)
        print(f"  {x['date']} {x['symbol']:<8} {x['dir']:<10} {x['exit_reason']:<16} "
              f"exit={x['exit_ret']*100:+.2f}% 毛${x['pnl']:+.0f} | "
              f"T0收={d['t0_incr_pct']}% T+1={d['t1_incr_pct']}% T+3={d['t3_incr_pct']}%")
    out["worst8"] = detail

    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(f"\n→ {OUT}")


if __name__ == "__main__":
    main()
