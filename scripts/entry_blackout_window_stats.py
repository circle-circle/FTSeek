#!/usr/bin/env python3
"""RFC-200 历史开盘时间桶只读 SQL 统计（非 bar 路径回放）。

口径固定：PLAYBOOK% 已平仓记录；净盈亏优先 pnl_net，否则
realized_pnl - fees_est。entry_time 按仓内既有 ET ISO 文本取 HH:MM。
结果只是历史描述，不是 09:45 政策的因果反事实或毕业证据。
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NET_EXPR = "COALESCE(pnl_net, realized_pnl - COALESCE(fees_est, 0))"
BUCKETS = (
    ("before_0930", 0, 570),
    ("open_0930_0935", 570, 575),
    ("open_0935_0945", 575, 585),
    ("open_0945_1000", 585, 600),
    ("open_1000_1100", 600, 660),
    ("after_1100", 660, 24 * 60),
)


def bucket_for(entry_time: str) -> str:
    minute = int(entry_time[11:13]) * 60 + int(entry_time[14:16])
    for name, lower, upper in BUCKETS:
        if lower <= minute < upper:
            return name
    raise ValueError(f"invalid entry_time: {entry_time!r}")


def load_rows(db_path: Path, from_date: str, to_date: str | None = None):
    uri = f"file:{db_path.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        sql = f"""
            SELECT trade_date, symbol, entry_time, exit_time, {NET_EXPR} AS net_pnl
            FROM trade_records
            WHERE trade_date >= ? AND source_type LIKE 'PLAYBOOK%'
        """
        params = [from_date]
        if to_date:
            sql += " AND trade_date <= ?"
            params.append(to_date)
        return [dict(row) for row in conn.execute(sql, params)]
    finally:
        conn.close()


def _daily_cluster(rows):
    by_day = defaultdict(float)
    for row in rows:
        by_day[row["trade_date"]] += float(row["net_pnl"])
    values = list(by_day.values())
    result = {"active_days": len(values)}
    if not values:
        return result
    mean = statistics.mean(values)
    result.update(mean_net_per_active_day=mean,
                  min_daily_net=min(values), max_daily_net=max(values))
    if len(values) > 1:
        se = statistics.stdev(values) / math.sqrt(len(values))
        result["normal95_mean_net"] = [mean - 1.96 * se, mean + 1.96 * se]
    return result


def summarize(rows) -> dict:
    groups = {name: [] for name, _, _ in BUCKETS}
    open_count = 0
    excluded_open = defaultdict(int)
    for row in rows:
        bucket = bucket_for(row["entry_time"])
        if row["exit_time"] is None or row["net_pnl"] is None:
            open_count += 1
            excluded_open[bucket] += 1
            continue
        groups[bucket].append(row)
    out = {}
    for name, _, _ in BUCKETS:
        items = groups[name]
        nets = [float(row["net_pnl"]) for row in items]
        out[name] = {
            "n": len(items),
            "days": len({row["trade_date"] for row in items}),
            "win_rate": (sum(net > 0 for net in nets) / len(nets)) if nets else None,
            "net_pnl": sum(nets),
            "mean_net_pnl": (sum(nets) / len(nets)) if nets else None,
            "excluded_open_or_no_pnl": excluded_open[name],
        }
    return {
        "schema": "rfc200_open_bucket_stats_v1",
        "closed_rows": sum(len(items) for items in groups.values()),
        "excluded_open_or_no_pnl": open_count,
        "buckets": out,
        "target_0935_0945_daily_cluster": _daily_cluster(groups["open_0935_0945"]),
        "net_pnl_convention": "pnl_net else realized_pnl-fees_est",
        "entry_time_convention": "ET ISO text HH:MM",
        "caveat": "historical description only; not a causal counterfactual or graduation verdict",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "ftseek_us.db")
    parser.add_argument("--from-date", default="2026-06-01")
    parser.add_argument("--to-date")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()
    result = summarize(load_rows(args.db, args.from_date, args.to_date))
    result["window"] = {"from": args.from_date, "to": args.to_date}
    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"window={result['window']} closed={result['closed_rows']} "
              f"excluded_open={result['excluded_open_or_no_pnl']}")
        for name, _, _ in BUCKETS:
            row = result["buckets"][name]
            print(f"{name}: n={row['n']} days={row['days']} "
                  f"net={row['net_pnl']:.2f} mean={row['mean_net_pnl']}")
        print(result["caveat"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
