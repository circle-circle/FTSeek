#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""质量反事实报表 (只读, 2026-10-18).

回答: 如果把 quality_shadow 的建议规则用于历史指令, fwd/执行净利会怎样?
  - all: 全量基线
  - exclude_high_score_long_downgrade: 排除 would_downgrade_high_score_long
  - require_market_confirmation: 排除 would_require_market_confirmation
  - prefer_medium_pullback: LONG 只保留 would_prefer_medium_pullback, SHORT 全保留
  - exclude_high_score_long: 排除 LONG + score_band HIGH

注意: 这是 cohort 反事实, 不是完整资金曲线/仓位回放; 只用于判断方向.
不写库、不调 LLM、不下单.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.quality_shadow_report import (
    DB,
    _stats,
    _tags,
    load_rows,
    load_trade_aggregates,
)


def included(tags: Dict[str, Any], rule: str) -> bool:
    direction = str(tags.get("direction") or "").upper()
    if rule == "all":
        return True
    if rule == "exclude_high_score_long_downgrade":
        return not bool(tags.get("would_downgrade_high_score_long"))
    if rule == "require_market_confirmation":
        return not bool(tags.get("would_require_market_confirmation"))
    if rule == "prefer_medium_pullback":
        if direction == "SHORT":
            return True
        return bool(tags.get("would_prefer_medium_pullback"))
    if rule == "exclude_high_score_long":
        return not (
            direction == "LONG" and str(tags.get("score_band") or "") == "HIGH")
    return False


def evaluate(rows, trades, rule: str) -> Dict[str, Any]:
    included_rows = []
    excluded_rows = []
    for row in rows:
        tags = _tags(row)
        if not tags:
            continue
        (included_rows if included(tags, rule) else excluded_rows).append((row, tags))
    fwd_vals = []
    net_bps_vals = []
    net_usd_vals = []
    for row, _ in included_rows:
        fwd = row["fwd_return_t1_open"]
        if fwd is not None:
            fwd_vals.append(float(fwd) * 10000)
        agg = trades.get(int(row["id"]))
        if agg and agg.get("net_bps") is not None:
            net_bps_vals.append(agg["net_bps"])
            net_usd_vals.append(agg["net"])
    missed_fwd = []
    missed_net = []
    for row, _ in excluded_rows:
        fwd = row["fwd_return_t1_open"]
        if fwd is not None and float(fwd) > 0:
            missed_fwd.append(float(fwd) * 10000)
        agg = trades.get(int(row["id"]))
        if agg and agg.get("net") is not None and agg["net"] > 0:
            missed_net.append(agg["net"])
    fwd_stats = _stats(fwd_vals)
    wins = sum(v for v in net_usd_vals if v > 0)
    losses = sum(v for v in net_usd_vals if v < 0)
    return {
        "rule": rule,
        "n": len(included_rows),
        "fwd_n": fwd_stats.get("n", 0),
        "fwd_bps": fwd_stats.get("mean_bps"),
        "fwd_win_pct": fwd_stats.get("win_pct"),
        "fwd_t": fwd_stats.get("t"),
        "exec_n": len(net_bps_vals),
        "net_bps": round(statistics.fmean(net_bps_vals), 2) if net_bps_vals else None,
        "net_usd": round(sum(net_usd_vals), 2),
        "net_win_pct": round(
            100.0 * sum(1 for v in net_usd_vals if v > 0) / len(net_usd_vals), 1
        ) if net_usd_vals else None,
        "pf": round(wins / abs(losses), 2) if losses else None,
        "excluded_n": len(excluded_rows),
        "missed_fwd_n": len(missed_fwd),
        "missed_fwd_bps": round(statistics.fmean(missed_fwd), 2) if missed_fwd else None,
        "missed_net_sum": round(sum(missed_net), 2) if missed_net else 0.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Quality shadow counterfactual report")
    parser.add_argument("--db", type=Path, default=DB)
    parser.add_argument("--days", type=int, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    rows = load_rows(args.db, args.days)
    trades = load_trade_aggregates(args.db)
    rules = [
        "all",
        "exclude_high_score_long_downgrade",
        "require_market_confirmation",
        "prefer_medium_pullback",
        "exclude_high_score_long",
    ]
    results = [evaluate(rows, trades, rule) for rule in rules]
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0
    header = [
        ("rule", "rule"), ("n", "n"), ("fwd_n", "fwd_n"), ("fwd_bps", "fwd_bps"),
        ("fwd_win%", "fwd_win_pct"), ("fwd_t", "fwd_t"), ("exec_n", "exec_n"),
        ("net_bps", "net_bps"), ("net_$", "net_usd"), ("win%", "net_win_pct"),
        ("PF", "pf"), ("excluded", "excluded_n"), ("missed_fwd_n", "missed_fwd_n"),
        ("missed_fwd_bps", "missed_fwd_bps"), ("missed_net_$", "missed_net_sum"),
    ]
    print("\t".join(display for display, _ in header))
    for row in results:
        print("\t".join(str(row.get(key)) for _, key in header))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
