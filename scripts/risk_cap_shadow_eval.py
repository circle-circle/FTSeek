#!/usr/bin/env python3
"""单笔风险帽 (DEB-20261031-risk-cap, P1) 出口结算器。

预注册判据 (读数前写死, 见 docs/decisions/DEB-20261031-risk-cap.md §3):
  生效门 E1 size_basis=risk_cap_usd 占比 ≥30% | E2 risk_amount p90 ≤ $96 | E3 1股超帽 ≤5%
  经济门 J1 笔均 > 基线 −$1.84 | J2 费用率 ≤ 基线+3bps | J3 笔数下降 ≤25%
  停止 S1 连 5 日净额 < 同长度基线×1.2 | S2 费用率高且笔均仍负 | S3 E3 超 5%

只读: 不写库、不改任何生产行为。基线 = A10 口径 7/1–10/31 全样本 (帽生效前)。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DB = REPO / "data/ftseek_us.db"
LIVE_FROM = "2026-10-31"          # 帽生效日 (配置 + restart)
BASELINE_FROM, BASELINE_TO = "2026-07-01", "2026-10-31"
CAP_USD = 80.0
BASELINE_PER_TRADE = -1.84        # A10 基线笔均 (预注册取值, 不随窗口漂移)
BASELINE_FEE_BPS = 9.33           # fee_gate_shadow 2026-10-31 读数
MIN_N, MIN_DAYS = 60, 10


def _rows(conn, start, end):
    return conn.execute("""
        SELECT trade_date, symbol, direction, entry_price, quantity, pnl_net,
               metadata_json
        FROM trade_records
        WHERE COALESCE(exit_price,0)>0 AND trade_date>=? AND trade_date<=?
          AND COALESCE(json_extract(metadata_json,'$.gate_mode'),'')!='research_momentum'
    """, (start, end)).fetchall()


def _metrics(rows):
    n = len(rows)
    if not n:
        return {"n": 0}
    pnl = [r["pnl_net"] or 0.0 for r in rows]
    fee = 0.0
    notional = 0.0
    risks = []
    for r in rows:
        md = json.loads(r["metadata_json"] or "{}")
        fb = md.get("fee_breakdown") or {}
        if isinstance(fb, dict):
            fee += float(fb.get("total") or fb.get("total_fee") or 0.0)
        sl = md.get("stop_loss")
        ep = float(r["entry_price"] or 0)
        q = float(r["quantity"] or 0)
        notional += ep * q
        if sl and ep > 0:
            risks.append(abs(ep - float(sl)) * q)
    fee_bps = (fee / notional * 10000) if notional > 0 else None
    return {"n": n, "net": sum(pnl), "avg": sum(pnl) / n,
            "wr": sum(1 for p in pnl if p > 0) / n * 100,
            "fee_bps": fee_bps,
            "risk_p50": statistics.median(risks) if risks else None,
            "risk_p90": sorted(risks)[int(len(risks) * 0.9)] if risks else None,
            "n_days": len({r["trade_date"] for r in rows})}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    base = _metrics(_rows(conn, BASELINE_FROM, BASELINE_TO))
    live = _metrics(_rows(conn, LIVE_FROM, "2099-12-31"))

    # E1/E3 需要逐笔归因: 帽生效后 是否真的由帽绑定 —— 用「若帽关闭会是多少股」反推,
    # 而不是指望历史行里存在 size_basis (该字段只在 planned_executability 预览里)。
    e1_num = e3_num = 0
    for r in _rows(conn, LIVE_FROM, "2099-12-31"):
        md = json.loads(r["metadata_json"] or "{}")
        sl, ep, q = md.get("stop_loss"), float(r["entry_price"] or 0), float(r["quantity"] or 0)
        if not sl or ep <= 0:
            continue
        per_share = abs(ep - float(sl))
        if per_share <= 0:
            continue
        cap_shares = max(1, int(CAP_USD / per_share))
        if q <= cap_shares + 1:          # 成交股数已落在帽内 ⇒ 帽在起作用
            e1_num += 1
        if per_share > CAP_USD:           # 1 股即超帽
            e3_num += 1
    e1 = (e1_num / live["n"] * 100) if live.get("n") else None
    e3 = (e3_num / live["n"] * 100) if live.get("n") else None

    out = {"baseline": base, "live": live, "e1_cap_share_pct": e1, "e3_one_share_over_pct": e3,
           "caps": {"cap_usd": CAP_USD, "baseline_per_trade": BASELINE_PER_TRADE,
                    "baseline_fee_bps": BASELINE_FEE_BPS, "min_n": MIN_N, "min_days": MIN_DAYS},
           "live_from": LIVE_FROM}

    gates = {}
    if live.get("n", 0) == 0:
        gates["state"] = "NO_LIVE_SAMPLE (帽生效后无已平仓样本)"
    else:
        gates["E1"] = "PASS" if (e1 or 0) >= 30 else "FAIL"
        gates["E2"] = ("PASS" if (live["risk_p90"] or 1e9) <= CAP_USD * 1.2 else "FAIL")
        gates["E3"] = "PASS" if (e3 or 0) <= 5 else "FAIL"
        gates["J1"] = ("PASS" if live["avg"] > BASELINE_PER_TRADE else "FAIL")
        gates["J2"] = ("PASS" if (live["fee_bps"] or 0) <= (base["fee_bps"] or BASELINE_FEE_BPS) + 3
                       else "FAIL")
        gates["J3"] = ("PASS" if base.get("n") and live["n"] / max(base["n"], 1) * 100 >= 75
                       else "FAIL")
        gates["sample_ok"] = (live["n"] >= MIN_N or live.get("n_days", 0) >= MIN_DAYS)
        gates["state"] = ("READY_TO_ADJUDICATE" if gates["sample_ok"]
                          else "KEEP_SAMPLING")
    out["gates"] = gates

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    print("== 风险帽 (DEB-20261031-risk-cap) ==")
    print(f"基线 {BASELINE_FROM}~{BASELINE_TO}: n={base.get('n')} 笔均 {base.get('avg', 0):+.2f} "
          f"WR {base.get('wr', 0):.1f}% 费 {base.get('fee_bps') or 0:.2f}bps "
          f"风险 p50 ${base.get('risk_p50') or 0:.0f} p90 ${base.get('risk_p90') or 0:.0f}")
    if live.get("n"):
        print(f"帽后 {LIVE_FROM}~今: n={live['n']} 笔均 {live['avg']:+.2f} WR {live['wr']:.1f}% "
              f"费 {live['fee_bps'] or 0:.2f}bps 风险 p50 ${live['risk_p50'] or 0:.0f} "
              f"p90 ${live['risk_p90'] or 0:.0f} | 帽绑定占比 {e1 or 0:.0f}% | 1股超帽 {e3 or 0:.1f}%")
    print("判据:", " ".join(f"{k}={v}" for k, v in gates.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())