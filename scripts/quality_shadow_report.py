#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Quality shadow 只读反馈报表 (2026-10-18).

用途: 按 source/event/evidence/debate/方向等维度, 对齐 instruction 的
T+1 方向调整收益与已执行净收益, 看哪一层真的有 edge.

边界:
  - 只读 data/ftseek_us.db; 不写任何表; 不调用 LLM; 不下单.
  - 历史行没有 metadata.quality_shadow 时, 现场用 build_quality_shadow 回算.
  - fwd 收益 = fwd_return_t1_open (direction-signed) * 10000 bps.
  - 执行净收益来自 trade_records 回连 metadata_json.playbook_instruction_id.

用法:
  PYTHONPATH=. ./venv/bin/python scripts/quality_shadow_report.py --group origin,direction
  ... --group evidence_tier --min-n 20 --json
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DB = ROOT / "data" / "ftseek_us.db"

from app.playbook.quality_shadow import build_quality_shadow

DEFAULT_GROUP = ["origin", "direction"]
DIMENSIONS = [
    "origin", "direction", "score_band", "evidence_tier", "confidence_basis",
    "info_channel", "debate_outcome", "entry_archetype", "event_family",
    "would_downgrade_high_score_long", "would_prefer_medium_pullback",
    "would_require_market_confirmation",
]


def _json_obj(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def _f(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _stats(values: Iterable[float]) -> Dict[str, Any]:
    vals = [v for v in values if v is not None]
    if not vals:
        return {"n": 0}
    mean = statistics.fmean(vals)
    tstat = None
    if len(vals) > 2:
        sd = statistics.pstdev(vals)
        if sd > 0:
            tstat = mean / (sd / math.sqrt(len(vals)))
    return {
        "n": len(vals),
        "mean_bps": round(mean, 2),
        "median_bps": round(statistics.median(vals), 2),
        "win_pct": round(100.0 * sum(1 for v in vals if v > 0) / len(vals), 1),
        "t": round(tstat, 2) if tstat is not None else None,
    }


def load_rows(db_path: Path, days: Optional[int]) -> List[sqlite3.Row]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    sql = """SELECT id, date, symbol, direction, confidence, executed,
                    fwd_return_t1_open, raw_instruction_json, metadata_json
             FROM playbook_instructions
             WHERE direction IN ('BUY', 'SELL_SHORT')"""
    params: List[Any] = []
    if days:
        sql += " AND date >= date('now', ?)"
        params.append(f"-{int(days)} days")
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows


def load_trade_aggregates(db_path: Path) -> Dict[int, Dict[str, float]]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT metadata_json, realized_pnl, pnl_net, fees_est, entry_price, quantity
           FROM trade_records
           WHERE exit_time IS NOT NULL AND exit_price > 0"""
    ).fetchall()
    conn.close()
    out: Dict[int, Dict[str, float]] = defaultdict(
        lambda: {"gross": 0.0, "net": 0.0, "fees": 0.0, "notional": 0.0})
    bad_instruction_ids = 0
    for row in rows:
        meta = _json_obj(row["metadata_json"])
        instruction_id = meta.get("playbook_instruction_id")
        try:
            instruction_id = int(instruction_id)
        except (TypeError, ValueError):
            bad_instruction_ids += 1
            continue
        notional = (_f(row["entry_price"]) or 0.0) * (_f(row["quantity"]) or 0.0)
        agg = out[instruction_id]
        agg["gross"] += _f(row["realized_pnl"]) or 0.0
        agg["net"] += _f(row["pnl_net"]) or 0.0
        agg["fees"] += _f(row["fees_est"]) or 0.0
        agg["notional"] += notional
    if bad_instruction_ids:
        print(
            f"[quality_shadow_report] {bad_instruction_ids} trade rows 缺有效 "
            f"playbook_instruction_id, 已跳过净利回连",
            file=sys.stderr,
        )
    for agg in out.values():
        notional = agg["notional"]
        if notional > 0:
            agg["gross_bps"] = agg["gross"] / notional * 10000
            agg["net_bps"] = agg["net"] / notional * 10000
            agg["fee_bps"] = agg["fees"] / notional * 10000
        else:
            agg["gross_bps"] = agg["net_bps"] = agg["fee_bps"] = None
    return out


def _tags(row: sqlite3.Row) -> Dict[str, Any]:
    metadata = _json_obj(row["metadata_json"])
    tags = metadata.get("quality_shadow")
    if isinstance(tags, dict) and tags:
        return tags
    inst = dict(row)
    inst["raw_instruction"] = _json_obj(row["raw_instruction_json"])
    return build_quality_shadow(inst)


def report(db_path: Path, group: List[str], days: Optional[int], min_n: int) -> List[dict]:
    rows = load_rows(db_path, days)
    trades = load_trade_aggregates(db_path)
    buckets: Dict[Tuple[str, ...], Dict[str, Any]] = {}
    for row in rows:
        tags = _tags(row)
        if not tags:
            continue
        key = tuple(str(tags.get(dim) if tags.get(dim) is not None else "UNKNOWN")
                    for dim in group)
        bucket = buckets.setdefault(key, {
            "group": key,
            "fwd": [],
            "net_bps": [],
            "gross_bps": [],
            "fee_bps": [],
            "net_usd": [],
        })
        fwd = _f(row["fwd_return_t1_open"])
        if fwd is not None:
            bucket["fwd"].append(fwd * 10000)
        agg = trades.get(int(row["id"]))
        if agg and agg.get("net_bps") is not None:
            bucket["net_bps"].append(agg["net_bps"])
            bucket["gross_bps"].append(agg["gross_bps"])
            bucket["fee_bps"].append(agg["fee_bps"])
            bucket["net_usd"].append(agg["net"])
    results = []
    for bucket in buckets.values():
        fwd = _stats(bucket["fwd"])
        net = _stats(bucket["net_bps"])
        if net["n"] < min_n:
            continue
        wins = sum(v for v in bucket["net_usd"] if v > 0)
        losses = sum(v for v in bucket["net_usd"] if v < 0)
        results.append({
            "group": bucket["group"],
            "n": len(bucket["fwd"]),
            "fwd_n": fwd.get("n", 0),
            "fwd_mean_bps": fwd.get("mean_bps"),
            "fwd_median_bps": fwd.get("median_bps"),
            "fwd_win_pct": fwd.get("win_pct"),
            "fwd_t": fwd.get("t"),
            "exec_n": net.get("n", 0),
            "gross_bps": round(statistics.fmean(bucket["gross_bps"]), 2) if bucket["gross_bps"] else None,
            "fee_bps": round(statistics.fmean(bucket["fee_bps"]), 2) if bucket["fee_bps"] else None,
            "net_bps": net.get("mean_bps"),
            "net_usd": round(sum(bucket["net_usd"]), 2),
            "net_win_pct": net.get("win_pct"),
            "pf": round(wins / abs(losses), 2) if losses else None,
        })
    results.sort(key=lambda x: (x["net_bps"] is None, -(x["net_bps"] or 0), x["group"]))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Quality shadow read-only feedback report")
    parser.add_argument("--db", type=Path, default=DB)
    parser.add_argument("--group", default=",".join(DEFAULT_GROUP),
                        help="逗号分隔维度: " + ", ".join(DIMENSIONS))
    parser.add_argument("--days", type=int, default=None)
    parser.add_argument("--min-n", type=int, default=0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    group = [g.strip() for g in args.group.split(",") if g.strip()]
    bad = [g for g in group if g not in DIMENSIONS]
    if bad:
        parser.error(f"unknown dimensions: {bad}; allowed: {DIMENSIONS}")
    results = report(args.db, group, args.days, args.min_n)
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0
    header = [
        ("group", "group"), ("n", "n"), ("fwd_n", "fwd_n"),
        ("fwd_bps", "fwd_mean_bps"), ("fwd_win%", "fwd_win_pct"), ("fwd_t", "fwd_t"),
        ("exec_n", "exec_n"), ("gross_bps", "gross_bps"), ("fee_bps", "fee_bps"),
        ("net_bps", "net_bps"), ("net_$", "net_usd"), ("win%", "net_win_pct"),
        ("PF", "pf"),
    ]
    print("\t".join(display for display, _ in header))
    for row in results:
        print("\t".join(str(row.get(key)) for _, key in header))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
