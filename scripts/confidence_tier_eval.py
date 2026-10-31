#!/usr/bin/env python3
"""confidence 分档 vs 毛利/R —— 「零边际人群被费用抽干」的检验器。

背景 (DEB-20261031-budget-design §10): 715 笔按 confidence 分档后出现一个结构现象 ——
  conf < 60 (n=408): 毛利/R = −0.001 ≈ 0, 净 −$2,261 ≈ 408 × 单笔费用 $5.5
  conf ≥ 60 (n=282): 毛利/R = +0.037, 净 +$1,014
两子期 (7-8月 / 9-10月) 同号。但 Welch t 仅 +1.29 (**不显著**) ⇒ 候选, 非结论。

预注册判据 (读数前写死):
  T1 两子期 R 差同号 (conf≥60 更高)
  T2 全期 Welch t ≥ 2.0
  T3 conf<60 组的毛利/R 绝对值 ≤ 0.01 且 样本 ≥300 (「零边际」结构成立)
  T1∧T2∧T3 ⇒ GATE_CANDIDATE (可提交为「conf<60 不进场」的硬门, 须另立 DEB)
  T1∧T3 但 T2 不过 ⇒ KEEP_SHADOW (继续攒样, 不许当门用)
  其余 ⇒ WEAK / 作废

只读: 不写库, 不改生产。
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DB = REPO / "data" / "ftseek_us.db"
SINCE = "2026-07-01"
SPLIT_DATE = "2026-09-01"
LOW_TIER_MAX = 60          # conf < 60 = 低档
MIN_N_LOW = 300
T_THRESHOLD = 2.0
ZERO_EDGE_BAND = 0.01      # |R| ≤ 0.01 视为「零边际」


def _load():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conf = {r["id"]: r["confidence"] for r in conn.execute(
        "SELECT id, confidence FROM playbook_instructions WHERE date>=?", (SINCE,))}
    rows = conn.execute("""
        SELECT trade_date, entry_price, quantity, pnl_net, metadata_json
        FROM trade_records
        WHERE COALESCE(exit_price,0)>0 AND trade_date>=?
          AND COALESCE(json_extract(metadata_json,'$.gate_mode'),'')!='research_momentum'
    """, (SINCE,)).fetchall()
    out = []
    for r in rows:
        md = json.loads(r["metadata_json"] or "{}")
        ep, q, sl = float(r["entry_price"] or 0), float(r["quantity"] or 0), md.get("stop_loss")
        if ep <= 0 or q <= 0 or not sl:
            continue
        ps = abs(ep - float(sl))
        if ps <= 0:
            continue
        fee = float((md.get("fee_breakdown") or {}).get("total") or 4.18)
        cf = conf.get(md.get("playbook_instruction_id"))
        if cf is None:
            continue
        out.append({"d": r["trade_date"], "cf": float(cf),
                    "R": ((r["pnl_net"] or 0) + fee) / (ps * q),
                    "net": r["pnl_net"] or 0, "fee": fee})
    return out


def _side(rows, low: bool):
    v = [x for x in rows if (x["cf"] < LOW_TIER_MAX) == low]
    if not v:
        return {"n": 0, "R": None, "net": 0.0}
    return {"n": len(v), "R": statistics.mean(x["R"] for x in v),
            "net": sum(x["net"] for x in v)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    D = _load()
    full_low, full_high = _side(D, True), _side(D, False)
    halves = {}
    for name, sub in (("A_7-8月", [x for x in D if x["d"] < SPLIT_DATE]),
                      ("B_9-10月", [x for x in D if x["d"] >= SPLIT_DATE])):
        lo, hi = _side(sub, True), _side(sub, False)
        halves[name] = {"low": lo, "high": hi,
                        "diff": (None if lo["R"] is None or hi["R"] is None
                                 else hi["R"] - lo["R"])}
    lo_r = [x["R"] for x in D if x["cf"] < LOW_TIER_MAX]
    hi_r = [x["R"] for x in D if x["cf"] >= LOW_TIER_MAX]
    t = None
    if len(lo_r) > 2 and len(hi_r) > 2:
        se = math.sqrt(statistics.variance(lo_r) / len(lo_r) + statistics.variance(hi_r) / len(hi_r))
        if se > 0:
            t = (statistics.mean(hi_r) - statistics.mean(lo_r)) / se

    diffs = [h["diff"] for h in halves.values() if h["diff"] is not None]
    t1 = bool(diffs) and (all(d > 0 for d in diffs) or all(d < 0 for d in diffs))
    t2 = t is not None and abs(t) >= T_THRESHOLD
    t3 = (full_low["n"] >= MIN_N_LOW and full_low["R"] is not None
          and abs(full_low["R"]) <= ZERO_EDGE_BAND)
    if t1 and t2 and t3:
        state = "GATE_CANDIDATE (须另立 DEB 才能上硬门)"
    elif t1 and t3:
        state = "KEEP_SHADOW (同号且零边际成立, 但 t 不过 ⇒ 不许当门用)"
    else:
        state = "WEAK / 作废"

    out = {"full": {"low": full_low, "high": full_high, "welch_t": t},
           "halves": halves, "checks": {"T1_split_same_sign": t1, "T2_t_ge_2": t2,
                                        "T3_zero_edge_low": t3},
           "state": state, "thresholds": {"low_tier_max": LOW_TIER_MAX,
                                          "t_threshold": T_THRESHOLD,
                                          "zero_edge_band": ZERO_EDGE_BAND}}
    try:
        p = REPO / "data/shadow/confidence_tier_latest.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    except OSError as exc:
        print(f"[warn] 写产物失败: {exc}", file=sys.stderr)

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    print("== confidence 分档检验 (conf<60 vs ≥60) ==")
    print(f"  conf<60 : n={full_low['n']:3d} 毛利/R {(full_low['R'] or 0):+.3f} 净 ${full_low['net']:+8,.0f}")
    print(f"  conf≥60 : n={full_high['n']:3d} 毛利/R {(full_high['R'] or 0):+.3f} 净 ${full_high['net']:+8,.0f}")
    for k, h in halves.items():
        print(f"  {k}: 差 {(h['diff'] or 0):+.3f} "
              f"(低档 n={h['low']['n']} R={(h['low']['R'] or 0):+.3f} / 高档 n={h['high']['n']} R={(h['high']['R'] or 0):+.3f})")
    print(f"  Welch t = {t:+.2f}" if t is not None else "  Welch t = n/a")
    print(f"  判据: T1={t1} T2={t2} T3={t3} ⇒ {state}")
    return 0


if __name__ == "__main__":
    sys.exit(main())