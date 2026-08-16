#!/usr/bin/env python3
"""admission_replay.py — 准入门历史回放 (只读, 零生产影响)。

用途 (2026-08-06, DEB-20260806-source-control):
  把历史成交按候选阈值组合过一遍准入门, 回答"若当时启用, 会拦掉哪些、净额如何变"。
  用于**定档**: 配置里所有阈值缺省为 0 (不拦), 必须先用本脚本看分布与反事实再定。

口径 (必须诚实):
  - 反事实 = 直接剔除被拦交易 (assume 不开仓), 不假设资金被复用到别的机会 →
    这是"止漏上界", 非"盈利预测"; 机会成本未计。
  - 费用用 trade_records.fees_est (fees_source='est', SIM 期唯一可行口径),
    与 fee_model 同源; est≠实收, LIVE 后须换 broker/reconciled。
  - 窗口敏感性必看: 本项目已出现过 35 日窗口结论在 60 日翻转的先例
    (conf>=86/gated 路径), 故默认同时输出 35/60 日两窗, 两窗不一致即不可采信。
  - MFE/MAE 回填不全: 2026-05 及更早 mae_pct 全为 0 → 默认窗口不超过 60 日。

用法:
  python3 scripts/admission_replay.py                 # 默认候选档位矩阵
  python3 scripts/admission_replay.py --windows 35,60
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "data", "ftseek_us.db")


def _fee_of(qty: float, price: float, direction: str, date: Optional[str]) -> float:
    try:
        from app.execution.fee_model import estimate_round_trip
        return float(estimate_round_trip(qty, price, price, direction, date)["total"])
    except Exception:
        return 3.98


def load(window_days: int) -> List[Dict[str, Any]]:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    q = f"""
    SELECT t.trade_date d, t.symbol, t.direction dir, t.entry_price ep, t.quantity qty,
           t.realized_pnl rp, t.fees_est fee, t.exit_reason er,
           i.take_profit tp, i.confidence conf,
           json_extract(i.raw_instruction_json,'$.source') src
    FROM trade_records t
    LEFT JOIN playbook_instructions i
      ON i.date = t.trade_date AND i.symbol = t.symbol
     AND i.direction = CASE t.direction WHEN 'LONG' THEN 'BUY'
                                        WHEN 'SHORT' THEN 'SELL_SHORT' ELSE t.direction END
    WHERE t.trade_date >= date('now', '-{int(window_days)} day')
      AND t.quantity > 0 AND t.entry_price > 0
    """
    rows = [dict(r) for r in con.execute(q)]
    con.close()
    for r in rows:
        r["notional"] = (r["ep"] or 0) * (r["qty"] or 0)
        r["net"] = (r["rp"] or 0) - (r["fee"] or 0)
        r["rt_fee"] = _fee_of(r["qty"], r["ep"], r["dir"], r["d"])
        r["fee_bps"] = 10000.0 * r["rt_fee"] / r["notional"] if r["notional"] else None
        if r.get("tp"):
            r["gross_at_tp"] = abs((r["tp"] or 0) - (r["ep"] or 0)) * (r["qty"] or 0)
            r["pf_ratio"] = r["gross_at_tp"] / r["rt_fee"] if r["rt_fee"] else None
        else:
            r["gross_at_tp"] = None
            r["pf_ratio"] = None
    return rows


def blocked(r: Dict[str, Any], g: Dict[str, float]) -> Optional[str]:
    if g.get("min_notional_usd") and r["notional"] < g["min_notional_usd"]:
        return "NOTIONAL"
    if g.get("min_entry_price") and (r["ep"] or 0) < g["min_entry_price"]:
        return "PRICE"
    if g.get("max_shares_per_order") and (r["qty"] or 0) > g["max_shares_per_order"]:
        return "SHARES"
    if g.get("min_profit_fee_ratio") and r["pf_ratio"] is not None \
            and r["pf_ratio"] < g["min_profit_fee_ratio"]:
        return "FEE_INEFF"
    if g.get("min_confidence") and (r["conf"] or 0) < g["min_confidence"]:
        return "LOW_CONF"
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--windows", default="35,60")
    args = ap.parse_args()
    windows = [int(x) for x in args.windows.split(",") if x.strip()]

    gates: List[Dict[str, Any]] = [
        {"_name": "baseline (无门)"},
        {"_name": "notional>=3000", "min_notional_usd": 3000},
        {"_name": "notional>=5000", "min_notional_usd": 5000},
        {"_name": "price>=30", "min_entry_price": 30},
        {"_name": "shares<=199", "max_shares_per_order": 199},
        {"_name": "pf_ratio>=6", "min_profit_fee_ratio": 6},
        {"_name": "pf_ratio>=10", "min_profit_fee_ratio": 10},
        {"_name": "conf>=70 (无IC证据)", "min_confidence": 70},
        {"_name": "notional>=5000 + price>=30", "min_notional_usd": 5000, "min_entry_price": 30},
    ]

    for w in windows:
        rows = load(w)
        base = sum(r["net"] for r in rows)
        print(f"\n{'='*78}\n窗口 {w} 日  n={len(rows)}  基线净额 ${base:+,.0f} "
              f"({base/max(1,len(rows)):+.2f}/笔)\n{'='*78}")
        print("%-28s %5s %5s %10s %10s %9s" % ("gate", "拦", "留", "留存净额$", "Δ vs base", "留存/笔"))
        for g in gates:
            name = g["_name"]
            kept = [r for r in rows if blocked(r, g) is None]
            nblk = len(rows) - len(kept)
            knet = sum(r["net"] for r in kept)
            per = knet / len(kept) if kept else 0.0
            print("%-28s %5d %5d %10.0f %10.0f %9.2f"
                  % (name, nblk, len(kept), knet, knet - base, per))
            # R1 复核建议 3: conf 门在两窗都有大额 Δ>0, 极易被「只看数字」误用为有效门。
            if "conf" in name:
                print("  ⚠ 无 IC 证据的假设门 (60日 n=242 实测 confidence IC=−0.021, t=−0.33)"
                      " — 禁止据此启用准入门")

        # 分布摸底 — 定档需要看清现状, 不猜
        print("\n-- 现状分布 (定档依据) --")
        for lab, key, buckets in [
            ("名义额", "notional", [0, 1500, 3000, 5000, 8000, 10**9]),
            ("股价", "ep", [0, 20, 30, 60, 150, 10**9]),
            ("股数", "qty", [0, 10, 25, 60, 199, 10**9]),
            ("fee_bps", "fee_bps", [0, 5, 10, 15, 25, 10**9]),
            ("pf_ratio", "pf_ratio", [0, 3, 6, 10, 20, 10**9]),
        ]:
            parts = []
            for i in range(len(buckets) - 1):
                lo, hi = buckets[i], buckets[i + 1]
                s = [r for r in rows if r.get(key) is not None and lo <= r[key] < hi]
                if not s:
                    continue
                net = sum(r["net"] for r in s)
                parts.append(f"[{lo:g},{hi:g}): n={len(s)} ${net:+.0f}")
            print(f"  {lab:9s} " + " | ".join(parts))

    print("\n注: 反事实=剔除被拦交易, 未计资金复用/机会成本 → 止漏上界, 非盈利预测。")
    print("    两窗结论不一致的档位不可采信 (本项目已有 35d→60d 翻转先例)。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
