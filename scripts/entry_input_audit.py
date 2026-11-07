#!/usr/bin/env python3
"""入场侧"输入改动"两项可回测审计 (DEB-20261106 §31)。

I2 证伪条件可观测性: DA 写了 invalidation, 系统也抽出了 `alite_inv_price`,
   但**它真的会在出场前被触发吗**? 按证伪价先出场是更好还是更差?
I3 催化剂质量与同质化: 按 thesis 首类催化剂分桶看经济性; 当日"同一催化剂挤了几只"
   是否与当日盈亏单调相关; 同标的+同催化剂重复是否衰减。

纪律:
  - 只读 DB + data/replay_bars; 无写入;
  - 判读沿用 §30 预注册: 日聚类 |t|>=2 ∧ 单笔/单日集中度 <50% ∧ 双时代同号 才算成立;
  - 分类器是关键词首命中, 存在错分; 结论只用于**方向判断**, 不用于逐笔决策。

用法:
  venv/bin/python scripts/entry_input_audit.py invalidation
  venv/bin/python scripts/entry_input_audit.py catalyst [--json]
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import os
import re
import sqlite3
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "ftseek_us.db"
BARS_DIRS = (ROOT / "data" / "replay_bars", ROOT / "data" / "replay_bars_1m")
ERA_SPLIT = "2026-09-01"
DEAD_MFE = 0.3

CATALYSTS = [
    ("财报业绩", r"财报|营收|EPS|业绩|净利|毛利率|季报"),
    ("合同订单", r"合同|订单|中标|采购|供货|合约"),
    ("评级目标价", r"评级|目标价|上调|下调|分析师|共识"),
    ("产品发布", r"发布|新品|产品|上线|获批|FDA|临床"),
    ("并购合作", r"并购|收购|合作|战略|入股|合资"),
    ("资金持仓", r"内部人|减持|增持|机构|持仓|回购"),
    ("事件政策", r"关税|制裁|政策|监管|诉讼|调查|袭击|罢工|大选"),
    ("技术形态", r"MA\d|均线|突破|回调|超跌|金叉|死叉|支撑|压力|量比|RSI"),
]


def jd(raw: Any) -> Dict[str, Any]:
    try:
        v = json.loads(raw or "{}")
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def load_bars(symbol: str, day: str) -> Optional[List[list]]:
    sym = symbol.replace("US.", "")
    for d in BARS_DIRS:
        for name in (f"US_{sym}_{day}.json", f"{sym}_{day}.json"):
            p = d / name
            if p.exists():
                return json.loads(p.read_text())
    return None


def minutes(ts: str) -> int:
    return int(ts[11:13]) * 60 + int(ts[14:16])


def load_trades() -> List[dict]:
    conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        """SELECT t.id, t.symbol, t.trade_date, t.entry_time, t.exit_time, t.direction,
                  t.entry_price, t.exit_price, t.quantity, t.pnl_net, t.mfe_pct,
                  t.exit_reason, t.metadata_json, i.thesis
           FROM trade_records t
           LEFT JOIN (SELECT decision_id, MAX(thesis) AS thesis FROM playbook_instructions
                       WHERE row_kind='instruction' GROUP BY decision_id) i
                  ON i.decision_id = t.decision_id
           WHERE t.trade_date >= '2026-07-01' AND t.realized_pnl IS NOT NULL
             AND t.decision_id LIKE 'DA-%'""")]
    conn.close()
    seen, out = set(), []
    for r in rows:
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        r["dead"] = 1 if (r["mfe_pct"] or 0) < DEAD_MFE else 0
        r["cat"] = classify(r["thesis"])
        out.append(r)
    return out


def classify(thesis: Optional[str]) -> str:
    if not thesis:
        return "无"
    for name, pat in CATALYSTS:
        if re.search(pat, thesis, re.I):
            return name
    return "其他"


def _day_stats(days: List[float]) -> Dict[str, float]:
    if not days:
        return {}
    m, sd = statistics.mean(days), statistics.pstdev(days)
    return {"n_days": len(days), "mean": round(m, 2), "sd": round(sd, 1),
            "t": round(m / (sd / math.sqrt(len(days))), 2) if sd else 0.0,
            "mde": round(2.802 * sd / math.sqrt(len(days)), 1) if sd else None}


# ── I2: 证伪条件 ───────────────────────────────────────────────────────────
def audit_invalidation(rows: List[dict]) -> dict:
    with_inv = [r for r in rows if jd(r["metadata_json"]).get("alite_inv_price")]
    recs, nobar = [], 0
    for r in with_inv:
        inv = float(jd(r["metadata_json"])["alite_inv_price"])
        bars = load_bars(r["symbol"], r["trade_date"])
        if not bars:
            nobar += 1
            continue
        sign = 1 if r["direction"] == "LONG" else -1
        q = r["quantity"] or 0
        t0 = minutes(r["entry_time"])
        t1 = minutes(r["exit_time"]) if r["exit_time"] else 10 ** 6
        crossed = None
        for b in bars:
            t = minutes(b[0])
            if t < t0:
                continue
            if t > t1:
                break
            if (sign > 0 and b[3] <= inv) or (sign < 0 and b[2] >= inv):
                crossed = inv
                break
        delta = sign * (crossed - r["exit_price"]) * q if crossed else 0.0
        recs.append({"day": r["trade_date"], "crossed": bool(crossed), "delta": delta,
                     "net": r["pnl_net"] or 0, "dead": r["dead"],
                     "inv_gap_pct": (inv / r["entry_price"] - 1) * 100})
    cr = [x for x in recs if x["crossed"]]
    by_day: Dict[str, float] = collections.defaultdict(float)
    for x in recs:
        by_day[x["day"]] += x["delta"]
    total = sum(x["delta"] for x in recs)
    stats = _day_stats(list(by_day.values()))
    return {
        "n_with_inv_price": len(with_inv), "n_scanned": len(recs), "n_nobar": nobar,
        "coverage_pct": round(100 * len(with_inv) / len(rows), 1),
        "trigger_rate_pct": round(100 * len(cr) / len(recs), 1) if recs else None,
        "dead_rate_when_crossed": round(sum(1 for x in cr if x["dead"]) / len(cr), 3) if cr else None,
        "dead_rate_when_not": round(sum(1 for x in recs if not x["crossed"] and x["dead"]) /
                                    max(1, len(recs) - len(cr)), 3),
        "baseline_dead_rate": round(sum(1 for x in recs if x["dead"]) / len(recs), 3) if recs else None,
        "median_inv_gap_pct": round(statistics.median(x["inv_gap_pct"] for x in recs), 2) if recs else None,
        "sum_delta_if_exit_at_inv": round(total, 2),
        "delta_day_stats": stats,
        "max_trade_share_pct": round(100 * abs(min(x["delta"] for x in recs)) / abs(total), 0) if total else None,
        "era_delta": {
            "7-8": round(sum(x["delta"] for x in recs if x["day"] < ERA_SPLIT), 2),
            "9-11": round(sum(x["delta"] for x in recs if x["day"] >= ERA_SPLIT), 2),
        },
    }


# ── I3: 催化剂 ─────────────────────────────────────────────────────────────
def audit_catalyst(rows: List[dict]) -> dict:
    by_cat: Dict[str, List[dict]] = collections.defaultdict(list)
    for r in rows:
        by_cat[r["cat"]].append(r)
    cats = []
    for k, v in sorted(by_cat.items(), key=lambda kv: -len(kv[1])):
        net = sum(x["pnl_net"] or 0 for x in v)
        cats.append({"cat": k, "n": len(v),
                     "dead_rate": round(sum(x["dead"] for x in v) / len(v), 3),
                     "net": round(net, 2), "per_trade": round(net / len(v), 2)})

    # 当日同催化剂聚集度
    by_day: Dict[str, List[dict]] = collections.defaultdict(list)
    for r in rows:
        by_day[r["trade_date"]].append(r)
    day_recs = []
    for d, v in by_day.items():
        cnt = collections.Counter(x["cat"] for x in v)
        top_cat, top_n = cnt.most_common(1)[0]
        day_recs.append({"day": d, "n": len(v), "mx": top_n, "top": top_cat,
                         "net": sum(x["pnl_net"] or 0 for x in v)})
    buckets = {}
    for lo, hi, lab in ((1, 2, "1-2"), (3, 4, "3-4"), (5, 99, "5+")):
        s = [x for x in day_recs if lo <= x["mx"] <= hi]
        if s:
            buckets[lab] = {"n_days": len(s), "n_trades": sum(x["n"] for x in s),
                            "per_trade": round(sum(x["net"] for x in s) / sum(x["n"] for x in s), 2),
                            "pos_days_pct": round(100 * sum(1 for x in s if x["net"] > 0) / len(s))}
    a = [x["net"] for x in day_recs if x["mx"] >= 5]
    b = [x["net"] for x in day_recs if x["mx"] < 5]
    welch = None
    if len(a) >= 5 and len(b) >= 5:
        diff = statistics.mean(a) - statistics.mean(b)
        se = math.sqrt(statistics.pvariance(a) / len(a) + statistics.pvariance(b) / len(b))
        welch = {"mean_5plus": round(statistics.mean(a), 2), "mean_rest": round(statistics.mean(b), 2),
                 "diff": round(diff, 2), "t": round(diff / se, 2) if se else None,
                 "n_days_5plus": len(a)}

    # 同标的+同催化剂 跨日重复
    seen: Dict[tuple, List[str]] = collections.defaultdict(list)
    rep_buckets: Dict[str, List[dict]] = collections.defaultdict(list)
    for r in sorted(rows, key=lambda x: (x["trade_date"], x["entry_time"] or "")):
        key = (r["symbol"], r["cat"])
        prior = [d for d in seen[key] if d < r["trade_date"]]
        rep_buckets["0" if not prior else ("1" if len(prior) == 1 else ">=2")].append(r)
        seen[key].append(r["trade_date"])
    repeat = {k: {"n": len(v), "dead_rate": round(sum(x["dead"] for x in v) / len(v), 3),
                  "per_trade": round(sum(x["pnl_net"] or 0 for x in v) / len(v), 2)}
              for k, v in rep_buckets.items()}
    return {"by_catalyst": cats, "day_homogeneity": buckets, "welch_5plus_vs_rest": welch,
            "repeat_symbol_catalyst": repeat}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=["invalidation", "catalyst"])
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    rows = load_trades()

    if a.what == "invalidation":
        res = audit_invalidation(rows)
        if a.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0
        print("== I2 证伪条件可观测性 ==")
        print("有可解析证伪价 %d / %d = %.0f%%（中位距入场 %.2f%%）" % (
            res["n_with_inv_price"], len(rows), res["coverage_pct"], res["median_inv_gap_pct"] or 0))
        print("出场前被穿越 %s%%  ⇒ **证伪价对 %.0f%% 的笔从未生效**" % (
            res["trigger_rate_pct"], 100 - (res["trigger_rate_pct"] or 0)))
        print("被穿越组死单率 %.0f%% vs 未穿越 %.0f%%（基线 %.0f%%）" % (
            100 * (res["dead_rate_when_crossed"] or 0), 100 * res["dead_rate_when_not"],
            100 * res["baseline_dead_rate"]))
        d = res["delta_day_stats"]
        print("若按证伪价先出场: ΣΔ %+.2f | 日均 %+.2f 日sd %.1f | t=%+.2f | 单笔最大占比 %s%%" % (
            res["sum_delta_if_exit_at_inv"], d.get("mean", 0), d.get("sd", 0), d.get("t", 0),
            res["max_trade_share_pct"]))
        print("双时代 ΣΔ 7-8月 %+.2f / 9-11月 %+.2f" % (
            res["era_delta"]["7-8"], res["era_delta"]["9-11"]))
        return 0

    res = audit_catalyst(rows)
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    print("== I3 催化剂经济性 (DA %d 笔) ==" % len(rows))
    print("  %-10s %5s %7s %10s %9s" % ("催化剂", "n", "死单%", "净额", "笔均"))
    for c in res["by_catalyst"]:
        print("  %-10s %5d %6.0f%% %10.2f %9.2f" % (
            c["cat"], c["n"], 100 * c["dead_rate"], c["net"], c["per_trade"]))
    print("\n== 当日同催化剂聚集度 ==")
    for lab, v in res["day_homogeneity"].items():
        print("  单催化剂最多 %-3s: %2d 天 / %3d 笔 | 笔均 %+7.2f | 盈利天 %d%%" % (
            lab, v["n_days"], v["n_trades"], v["per_trade"], v["pos_days_pct"]))
    w = res["welch_5plus_vs_rest"]
    if w:
        print("  5+ vs 其余: 日均 %+.2f vs %+.2f | diff %+.2f | Welch t=%+.2f (n=%d 天)" % (
            w["mean_5plus"], w["mean_rest"], w["diff"], w["t"], w["n_days_5plus"]))
    print("\n== 同标的+同催化剂 跨日重复 ==")
    for k, v in sorted(res["repeat_symbol_catalyst"].items()):
        print("  重复 %-3s: n=%3d 死单 %.0f%% 笔均 %+.2f" % (k, v["n"], 100 * v["dead_rate"], v["per_trade"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())