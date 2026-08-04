#!/usr/bin/env python3
"""隔夜延续分析 (DEB-20260805 门D 预注册依据, 2026-08-05, 只读).

问题: 若当日不平仓, 持有到 T+1 收盘, 增量收益 (方向调整) 是否显著 >0?
  按 方向×退出原因 切片, 重点检验"SHORT 赢家隔夜延续"假说 (盘中快查 n=7 均值+3.53%)。

口径:
  exit_ret  = sign × (exit_price - entry_price) / entry_price   (实盘已实现, 毛)
  t1_ret    = fwd_return_t1 (ic_tracker 回填, sign × (close_T+1 - entry)/entry)
  incr      = (1+t1)/(1+exit_ret) - 1  ≈ sign × (close_T+1 - exit)/exit  (隔夜增量, 毛)
  费用: 隔夜不多一次平仓 (笔数不变) → 费用中性; SHORT 隔夜 borrow 成本另计
        (borrow_rate_snapshot 在产, 大盘股量级 <1bp/日, 此处不扣, 判读时留意).

统计: 遵循 README 预注册门径规范 — 双侧 null-pooled bootstrap (数据中心化后置换重抽
  模拟 H0: 增量均值=0), B=10,000; 报告 效应量 + CI95 + p 三列。见数后不再放宽判据。

输出: 控制台表 + data/overnight_continuation_latest.json
"""
import json
import random
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "ftseek_us.db"
OUT = ROOT / "data" / "overnight_continuation_latest.json"
DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 30
B = 10_000
MIN_N = 5


def null_pooled_bootstrap(vals):
    """双侧 null-pooled: H0=均值0. 返回 (mean, ci_lo, ci_hi, p)."""
    n = len(vals)
    mean = sum(vals) / n
    centered = [v - mean for v in vals]
    rng = random.Random(20260805)
    boot = []
    for _ in range(B):
        s = sum(centered[rng.randrange(n)] for _ in range(n)) / n
        boot.append(s + mean)  # CI 用普通 bootstrap 分布
    boot.sort()
    ci_lo, ci_hi = boot[int(0.025 * B)], boot[int(0.975 * B)]
    # null 分布 (中心化后均值围绕 0)
    rng2 = random.Random(20260805 + 1)
    null_means = sorted(
        sum(centered[rng2.randrange(n)] for _ in range(n)) / n for _ in range(B)
    )
    p = 2 * sum(1 for m in null_means if abs(m) >= abs(mean)) / B
    return mean, ci_lo, ci_hi, min(p, 1.0)


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT date, symbol, direction, entry_price, exit_price, exit_reason,
                  fwd_return_t1, fwd_return_t3, pnl
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
        t1 = float(r["fwd_return_t1"])
        incr = (1 + t1) / (1 + exit_ret) - 1 if exit_ret > -0.99 else None
        if incr is None:
            continue
        recs.append({
            "date": r["date"], "symbol": r["symbol"], "dir": r["direction"],
            "exit_reason": r["exit_reason"], "exit_ret": exit_ret,
            "t1": t1, "t3": r["fwd_return_t3"], "incr": incr, "pnl": r["pnl"],
        })

    print(f"样本: {len(recs)} 笔 (近{DAYS}日, 已成交且有T+1回填)\n")

    def cell(name, sub):
        if len(sub) < MIN_N:
            if sub:
                print(f"{name:<34} n={len(sub)} (<{MIN_N}, 不判)")
            return None
        incrs = [x["incr"] for x in sub]
        mean, lo, hi, p = null_pooled_bootstrap(incrs)
        realized = sum(x["pnl"] or 0 for x in sub)
        win = sum(1 for v in incrs if v > 0) / len(incrs)
        print(f"{name:<34} n={len(sub):>3}  增量均值={mean*100:+6.2f}%  "
              f"CI95[{lo*100:+.2f},{hi*100:+.2f}]  p={p:.4f}  正值率={win:.0%}  "
              f"实盘毛=${realized:+.0f}")
        return {"n": len(sub), "incr_mean": round(mean, 5), "ci95": [round(lo, 5), round(hi, 5)],
                "p": round(p, 4), "pos_rate": round(win, 3), "realized_gross": round(realized, 2)}

    out = {"generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
           "window_days": DAYS, "n_total": len(recs), "cells": {}}

    print("── 全部 ──")
    out["cells"]["all"] = cell("全部已成交单", recs)
    print("\n── 方向 × 退出原因 ──")
    for d in ("BUY", "SELL_SHORT"):
        for er in ("SL_TRAIL_LOCK", "PRE_CLOSE", "SL_BREAKEVEN", "MFE_STALL_EXIT"):
            sub = [x for x in recs if x["dir"] == d and x["exit_reason"] == er]
            key = f"{d}×{er}"
            r = cell(key, sub)
            if r:
                out["cells"][key] = r
    print("\n── 赢家/输家 × 方向 (exit_ret 正负) ──")
    for d in ("BUY", "SELL_SHORT"):
        for wname, wcond in (("赢家", lambda x: x["exit_ret"] > 0), ("输家", lambda x: x["exit_ret"] <= 0)):
            sub = [x for x in recs if x["dir"] == d and wcond(x)]
            r = cell(f"{d}·{wname}", sub)
            if r:
                out["cells"][f"{d}_{wname}"] = r

    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(f"\n→ {OUT}")


if __name__ == "__main__":
    main()
