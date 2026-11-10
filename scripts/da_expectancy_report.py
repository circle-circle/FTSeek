#!/usr/bin/env python3
"""Read-only DA/EXT expectancy report with A9 dual calibers and daily clustering."""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path

from scipy.stats import t as student_t

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.db_utils import a9_clean_where, era_clean_where  # noqa: E402

DB_PATH = ROOT / "data" / "ftseek_us.db"
MDE80_Z = 1.959963984540054 + 0.8416212335729143
SOURCE_PREDICATES = {
    "DA": "decision_id LIKE 'DA-%'",
    "EXT": "decision_id LIKE 'EXT-%'",
}


def daily_cluster_bps(rows: list[dict]) -> dict:
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["trade_date"]].append(row["net_bps"])
    daily_means = [statistics.fmean(values) for _, values in sorted(grouped.items())]
    days = len(daily_means)
    if not days:
        return {"days": 0, "mean_bps": None, "t": None, "ci80_low": None,
                "ci80_high": None, "mde80_bps": None}
    mean = statistics.fmean(daily_means)
    if days == 1:
        return {"days": 1, "mean_bps": round(mean, 4), "t": None,
                "ci80_low": None, "ci80_high": None, "mde80_bps": None}
    standard_error = statistics.stdev(daily_means) / math.sqrt(days)
    if standard_error == 0:
        return {"days": days, "mean_bps": round(mean, 4), "t": None,
                "ci80_low": round(mean, 4), "ci80_high": round(mean, 4),
                "mde80_bps": 0.0}
    critical = float(student_t.ppf(0.90, days - 1))
    return {
        "days": days,
        "mean_bps": round(mean, 4),
        "t": round(mean / standard_error, 4),
        "ci80_low": round(mean - critical * standard_error, 4),
        "ci80_high": round(mean + critical * standard_error, 4),
        "mde80_bps": round(MDE80_Z * standard_error, 4),
    }


def summarize(rows: list[dict]) -> dict:
    eligible = [row for row in rows if row["fees_est"] is not None]
    coverage = len(eligible) / len(rows) if rows else None
    if not eligible:
        return {"n": 0, "rows_before_fee_filter": len(rows),
                "fee_coverage": coverage, "daily_cluster": daily_cluster_bps([])}
    gross = [float(row["realized_pnl"]) for row in eligible]
    fees = [float(row["fees_est"]) for row in eligible]
    nets = [float(row["net"]) for row in eligible]
    notionals = [float(row["notional"]) for row in eligible]
    wins = [value for value in nets if value > 0]
    losses = [value for value in nets if value < 0]
    avg_win = statistics.fmean(wins) if wins else None
    avg_loss = statistics.fmean(losses) if losses else None
    breakeven = None
    if avg_win is not None and avg_loss is not None and avg_win > avg_loss:
        breakeven = abs(avg_loss) / (avg_win + abs(avg_loss))
    profit_factor = None
    if wins and losses and sum(losses) < 0:
        profit_factor = sum(wins) / abs(sum(losses))
    notional_sum = sum(notionals)
    clustered_rows = [dict(row, net_bps=row["net"] / row["notional"] * 10000.0)
                      for row in eligible]
    return {
        "n": len(eligible),
        "rows_before_fee_filter": len(rows),
        "fee_coverage": round(coverage, 4) if coverage is not None else None,
        "notional_usd": round(notional_sum, 2),
        "gross_usd": round(sum(gross), 2),
        "fees_usd": round(sum(fees), 2),
        "net_usd": round(sum(nets), 2),
        "gross_bps": round(sum(gross) / notional_sum * 10000.0, 4),
        "fee_bps": round(sum(fees) / notional_sum * 10000.0, 4),
        "net_bps": round(sum(nets) / notional_sum * 10000.0, 4),
        "net_win_rate": round(len(wins) / len(eligible), 4),
        "avg_net_win_usd": round(avg_win, 2) if avg_win is not None else None,
        "avg_net_loss_usd": round(avg_loss, 2) if avg_loss is not None else None,
        "breakeven_win_rate": round(breakeven, 4) if breakeven is not None else None,
        "profit_factor": round(profit_factor, 4) if profit_factor is not None else None,
        "daily_cluster": daily_cluster_bps(clustered_rows),
    }


def fetch_rows(conn: sqlite3.Connection, date_from: str, date_to: str,
               source: str, clean: bool) -> list[dict]:
    where = [
        "source_type = 'PLAYBOOK_SIM'",
        "exit_price > 0",
        "COALESCE(pnl_net, realized_pnl) IS NOT NULL",
        "realized_pnl IS NOT NULL",
        "entry_price > 0",
        "quantity > 0",
        "trade_date >= ?",
        "trade_date <= ?",
        SOURCE_PREDICATES[source],
    ]
    suffix = era_clean_where(conn)
    if clean:
        suffix += a9_clean_where(conn)
    sql = (
        "SELECT id, trade_date, decision_id, entry_price, quantity, realized_pnl,"
        " fees_est, COALESCE(pnl_net, realized_pnl) AS net"
        " FROM trade_records WHERE " + " AND ".join(where) + suffix + " ORDER BY id"
    )
    rows = []
    for record in conn.execute(sql, (date_from, date_to)).fetchall():
        row = dict(record)
        row["notional"] = abs(float(row["entry_price"]) * float(row["quantity"]))
        rows.append(row)
    return rows


def build_report(conn: sqlite3.Connection, date_from: str, date_to: str,
                 source: str) -> dict:
    clean_rows = fetch_rows(conn, date_from, date_to, source, clean=True)
    full_rows = fetch_rows(conn, date_from, date_to, source, clean=False)
    clean = summarize(clean_rows)
    including_excluded = summarize(full_rows)
    warnings = []
    if clean["n"] < 120:
        warnings.append(f"n={clean['n']} < 120：OKR 样本门未达，只报描述性读数")
    coverage = clean.get("fee_coverage")
    if coverage is not None and coverage < 0.95:
        warnings.append(f"费用覆盖率 {coverage:.2%} < 95%")
    return {
        "window": {"from": date_from, "to": date_to, "source": source,
                   "source_type": "PLAYBOOK_SIM"},
        "clean": clean,
        "including_excluded": including_excluded,
        "warnings": warnings,
        "method": {
            "net": "COALESCE(pnl_net, realized_pnl)",
            "bps": "sum(USD) / sum(abs(entry_price * quantity)) * 10000",
            "daily_cluster": "equal-weight daily means of per-trade net bps; Student-t 80% CI; MDE80=2.8016*SE",
            "a10": era_clean_where(conn).strip(),
        },
    }


def print_block(label: str, block: dict) -> None:
    if not block.get("n"):
        print(f"{label}: n=0")
        return
    cluster = block["daily_cluster"]
    print(
        f"{label}: n={block['n']} | 毛 {block['gross_bps']:.2f}bps | "
        f"费 {block['fee_bps']:.2f}bps | 净 {block['net_bps']:.2f}bps | "
        f"net=${block['net_usd']:.2f}"
    )
    print(
        f"  胜率={block['net_win_rate']:.1%} 盈亏平衡胜率="
        f"{block['breakeven_win_rate']:.1%} 利润因子={block['profit_factor']:.2f} "
        f"平均赢/亏=${block['avg_net_win_usd']:.2f}/${block['avg_net_loss_usd']:.2f}"
        if block["breakeven_win_rate"] is not None and block["profit_factor"] is not None
        else f"  胜率={block['net_win_rate']:.1%} 盈亏分解不足"
    )
    print(
        f"  日聚类 days={cluster['days']} mean={cluster['mean_bps']}bps "
        f"80%CI=[{cluster['ci80_low']},{cluster['ci80_high']}] "
        f"MDE80={cluster['mde80_bps']}bps t={cluster['t']}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="date_from", default="2026-10-06")
    parser.add_argument("--to", dest="date_to", default="2099-12-31")
    parser.add_argument("--source", choices=sorted(SOURCE_PREDICATES), default="DA")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    conn = sqlite3.connect(args.db.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        result = build_report(conn, args.date_from, args.date_to, args.source)
    finally:
        conn.close()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"DA/EXT 期望报表 {args.source} [{args.date_from}~{args.date_to}]")
        print_block("A9 clean", result["clean"])
        print_block("含测试批", result["including_excluded"])
        for warning in result["warnings"]:
            print(f"[WARN] {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
