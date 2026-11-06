#!/usr/bin/env python3
"""P1: DA score → 实现净额 校准表 (DEB-20261106-score-calibration-and-entry-quality §P1)。

预注册要点 (跑之前已写死, 不得事后改):
  · 分桶 = score 的实际取值支撑 {<6, 6.0, 6.5, 7.0, 7.5, 8.0}
    (2026-11-06 实测分布: 6.0×316 / 7.0×315 / 8.0×322 / 6.5×2 / 7.5×1 / 4.0×1 —— 离散支撑, 故用等值桶)
  · 关联键 = decision_id (非空); trades 每 did 基本 1 笔, instructions 同 did 取最高 score
  · 时效 = A10 era-clean (trade_date >= 2026-07-01, 剔 POSITION_SYNC/MANUAL, exit_price>0)
  · 判据: ①每桶 n>=30 ②Spearman(score,pnl_net) rho>0 且日聚类 CI95 不含 0
          ③首尾桶(均 n>=30)期望差 CI95 不含 0 且 >= 一笔往返费用(11.54bps × 中位名义)
  · 全部不过 ⇒ score 降级为叙述字段

只读: 仅 SELECT。用法:
  cd /opt/ftseek/FTSeek-US && venv/bin/python3.11 scripts/p1_score_calibration.py [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from app.core.db_utils import ERA_CLEAN_PERFORMANCE_WHERE  # noqa: E402

BIN_LABELS = ["<6", "6.0", "6.5", "7.0", "7.5", "8.0"]
FEE_BPS = 11.54
SEED = 20261106
BOOT = 2000


def bin_of(score: float) -> str:
    s = round(float(score), 1)
    if s < 6:
        return "<6"
    for label, v in (("6.0", 6.0), ("6.5", 6.5), ("7.0", 7.0), ("7.5", 7.5)):
        if s == v:
            return label
    return "8.0" if s >= 8 else "<6"


def spearman(xs, ys) -> float:
    def rank(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        rk = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                rk[order[k]] = avg
            i = j + 1
        return rk
    rx, ry = rank(xs), rank(ys)
    n = len(xs)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return num / den if den else float("nan")


def day_cluster_ci(rows, stat_fn, seed=SEED, iters=BOOT):
    """按 trade_date 重采样(日聚类), 返回 (lo, hi)。"""
    by_day = defaultdict(list)
    for r in rows:
        by_day[r["trade_date"]].append(r)
    days = list(by_day)
    rng = random.Random(seed)
    vals = []
    for _ in range(iters):
        sample = []
        for _ in range(len(days)):
            sample.extend(by_day[days[rng.randrange(len(days))]])
        v = stat_fn(sample)
        if v == v:  # not NaN
            vals.append(v)
    if not vals:
        return (float("nan"), float("nan"))
    vals.sort()
    return (vals[int(0.025 * len(vals))], vals[int(0.975 * len(vals)) - 1])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "ftseek_us.db"))
    ap.add_argument("--json", default=None)
    args = ap.parse_args(argv)
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    c = con.cursor()

    scores = {}
    rows_instr = 0
    for did, raw in c.execute(
            "select decision_id, raw_instruction_json from playbook_instructions "
            "where row_kind='instruction' and date >= '2026-07-01' "
            "and coalesce(decision_id,'') <> ''"):
        rows_instr += 1
        try:
            sc = json.loads(raw or "{}").get("score")
        except Exception:
            sc = None
        if sc is None:
            continue
        sc = float(sc)
        if did not in scores or sc > scores[did]:
            scores[did] = sc

    trades = list(c.execute(
        f"select decision_id, trade_date, symbol, direction, entry_session, "
        f"pnl_net, realized_pnl, fees_est, entry_price, quantity "
        f"from trade_records where exit_price > 0 and coalesce(decision_id,'') <> '' "
        f"{ERA_CLEAN_PERFORMANCE_WHERE}"))

    rows = []
    for did, tdate, sym, direction, session, net, gross, fees, px, qty in trades:
        if did not in scores:
            continue
        rows.append({"decision_id": did, "trade_date": tdate, "symbol": sym,
                     "direction": direction, "session": session,
                     "score": scores[did], "bin": bin_of(scores[did]),
                     "pnl_net": net or 0.0, "gross": gross or 0.0, "fees": fees or 0.0,
                     "notional": (px or 0) * (qty or 0)})

    n = len(rows)
    print(f"== P1 score 校准 (A10) — instruction 行 {rows_instr}, 有 score 的 did {len(scores)}, "
          f"成交且能关联 {n} / 765")
    if n < 30:
        print("样本不足, 结论 UNPOWERED")
        return 1

    buckets = defaultdict(list)
    for r in rows:
        buckets[r["bin"]].append(r)
    print("\n分桶 | n | 胜率 | 均值$ | 中位$ | 毛$ | 费$")
    stat = {}
    for label in BIN_LABELS:
        v = buckets.get(label) or []
        if not v:
            continue
        nets = [x["pnl_net"] for x in v]
        stat[label] = {
            "n": len(v),
            "win": sum(1 for x in nets if x > 0) / len(v),
            "mean": st.mean(nets), "median": st.median(nets),
            "gross": sum(x["gross"] for x in v), "fees": sum(x["fees"] for x in v),
            "sum": sum(nets),
        }
        s = stat[label]
        print(f"{label:4s} | {s['n']:4d} | {s['win']:.1%} | {s['mean']:8.2f} | {s['median']:7.2f} | "
              f"{s['gross']:9.2f} | {s['fees']:8.2f}")

    xs = [r["score"] for r in rows]
    ys = [r["pnl_net"] for r in rows]
    rho = spearman(xs, ys)
    lo, hi = day_cluster_ci(rows, lambda s: spearman([r["score"] for r in s],
                                                     [r["pnl_net"] for r in s]))
    print(f"\nSpearman rho = {rho:+.3f}  日聚类 CI95 [{lo:+.3f}, {hi:+.3f}]  (n={n}, 日数={len({r['trade_date'] for r in rows})})")

    eligible = [lb for lb in BIN_LABELS if (stat.get(lb) or {}).get("n", 0) >= 30]
    edge = None
    if len(eligible) >= 2:
        lo_b, hi_b = eligible[0], eligible[-1]
        d = stat[hi_b]["mean"] - stat[lo_b]["mean"]

        def _diff(s):
            a = [r["pnl_net"] for r in s if r["bin"] == hi_b]
            b = [r["pnl_net"] for r in s if r["bin"] == lo_b]
            if not a or not b:
                return float("nan")
            return sum(a) / len(a) - sum(b) / len(b)

        dlo, dhi = day_cluster_ci(rows, _diff)
        med_notional = st.median([r["notional"] for r in rows if r["notional"] > 0]) if rows else 0
        threshold = FEE_BPS / 10000.0 * med_notional
        edge = {"high_bin": hi_b, "low_bin": lo_b, "diff": d, "ci": [dlo, dhi],
                "fee_threshold": threshold, "median_notional": med_notional}
        print(f"首尾桶差 ({hi_b} − {lo_b}) = {d:+.2f}  日聚类 CI95 [{dlo:+.2f}, {dhi:+.2f}]  "
              f"| 门槛(一笔往返费) = {threshold:.2f}")

    # 判据
    crit = {
        "c1_each_bin_n30": all((stat.get(lb) or {}).get("n", 0) >= 30 for lb in eligible) if eligible else False,
        "c1_note": f"eligible bins={eligible}; 不足30的桶={[lb for lb in BIN_LABELS if (stat.get(lb) or {'n':0})['n'] < 30]}",
        "c2_rho_positive_ci_excludes_0": (rho > 0 and lo > 0),
        "c3_endpoint_ci_excludes_0_and_above_fee": bool(edge) and (edge["ci"][0] > 0 and edge["diff"] >= edge["fee_threshold"]),
    }
    print("\n判据:")
    for k, v in crit.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    verdict = "SCORE_USABLE" if (crit["c1_each_bin_n30"] and crit["c2_rho_positive_ci_excludes_0"]
                                 and crit["c3_endpoint_ci_excludes_0_and_above_fee"]) else "SCORE_DEMOTE_TO_NARRATIVE"
    print(f"\n裁决(预注册判据机器判定): {verdict}")

    out = {"schema": "p1_score_calibration_v1", "n": n, "rows_instr": rows_instr,
           "buckets": stat, "spearman": {"rho": rho, "ci95": [lo, hi]},
           "endpoint": edge, "criteria": crit, "verdict": verdict,
           "fee_bps": FEE_BPS, "seed": SEED, "bootstrap_iters": BOOT}
    if args.json:
        Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"-> {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())