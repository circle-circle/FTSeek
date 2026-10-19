#!/usr/bin/env python3
"""ext_entry_exec_price_shadow 只读读数 (2026-10-19).

预注册 (DEB-20260928-ext-exec-price50-shadow §三):
  观测窗 ≥3 交易日 / ≥60 行; 报告 ①candidates/current/shadow 池中位数与分布
  ②每轮 $5→$50 候选池缩减比例 ③实际 EXT 成交中 entry_price<$50 的占比
  ④影子价池与实际下单/成交的交集. 禁止自动改 EXT_ENTRY_MIN_PRICE; 切换生产须另立 DEB.

只读: JSONL 只读 + SQLite mode=ro; 只写 out JSON.
用法: PYTHONPATH=. venv/bin/python scripts/ext_entry_exec_price_eval.py
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
from collections import Counter
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_JSONL = REPO / "data" / "ext_entry_exec_price_shadow.jsonl"
DEFAULT_DB = REPO / "data" / "ftseek_us.db"
DEFAULT_OUT = REPO / "data" / "shadow" / "ext_entry_exec_price_eval_latest.json"
SINCE = "2026-09-28"
MIN_DAYS = 3
MIN_ROWS = 60


def load_rows(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _q(values: list[float], p: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * p))]


def _price_bucket(p: float) -> str:
    if p < 5:
        return "<5"
    if p < 10:
        return "5-10"
    if p < 20:
        return "10-20"
    if p < 50:
        return "20-50"
    return ">=50"


def compute_metrics(rows: list[dict]) -> dict:
    days = sorted({r.get("trade_date") for r in rows if r.get("trade_date")})
    sessions = Counter(r.get("session") for r in rows)

    def dist(key: str) -> dict:
        vals = [float(r[key]) for r in rows if isinstance(r.get(key), (int, float))]
        return {
            "n": len(vals),
            "median": round(_q(vals, 0.50), 2) if vals else None,
            "p90": round(_q(vals, 0.90), 2) if vals else None,
            "min": min(vals) if vals else None,
            "max": max(vals) if vals else None,
            "mean": round(statistics.mean(vals), 2) if vals else None,
        }

    shrink = [1 - (r["shadow_execution_pool_n"] / r["current_price_pool_n"])
              for r in rows if r.get("current_price_pool_n")]
    pre = [x for r in rows for x in (r.get("prefiltered_out") or [])]
    shadow = [x for r in rows for x in (r.get("shadow_execution_pool") or [])]

    def entries(arr: list[dict]) -> dict:
        prices = [float(x["decision_price"]) for x in arr if isinstance(x.get("decision_price"), (int, float))]
        chg = [float(x["change_pct"]) for x in arr if isinstance(x.get("change_pct"), (int, float))]
        return {
            "entries": len(arr),
            "unique_symbols": len({x.get("symbol") for x in arr if x.get("symbol")}),
            "price_median": round(_q(prices, 0.50), 2) if prices else None,
            "price_p10": round(_q(prices, 0.10), 2) if prices else None,
            "price_p90": round(_q(prices, 0.90), 2) if prices else None,
            "change_median_pct": round(_q(chg, 0.50), 2) if chg else None,
            "price_buckets": dict(Counter(_price_bucket(p) for p in prices)),
            "top_symbols": dict(Counter(x.get("symbol") for x in arr if x.get("symbol")).most_common(10)),
        }

    return {
        "rows": len(rows),
        "distinct_days": len(days),
        "first_day": days[0] if days else None,
        "last_day": days[-1] if days else None,
        "sessions": dict(sessions),
        "candidates_n": dist("candidates_n"),
        "current_price_pool_n": dist("current_price_pool_n"),
        "shadow_execution_pool_n": dist("shadow_execution_pool_n"),
        "prefiltered_out_n": dist("prefiltered_out_n"),
        "shrink_ratio": {
            "median": round(_q(shrink, 0.50), 4) if shrink else None,
            "mean": round(statistics.mean(shrink), 4) if shrink else None,
        },
        "prefiltered_out": entries(pre),
        "shadow_execution_pool": entries(shadow),
    }


def compute_actual_trades(db_path: Path, since: str = SINCE) -> dict:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, symbol, trade_date, entry_session, entry_price, "
        "COALESCE(pnl_net, realized_pnl, 0) AS net "
        "FROM trade_records WHERE source_type='PLAYBOOK_SIM' AND exit_price>0 "
        "AND trade_date>=? AND entry_session IN ('premarket','afterhours')",
        (since,),
    ).fetchall()
    con.close()
    low = [r for r in rows if (r["entry_price"] or 0) < 50]
    high = [r for r in rows if (r["entry_price"] or 0) >= 50]

    def summarize(arr) -> dict:
        nets = [float(r["net"] or 0.0) for r in arr]
        return {
            "n": len(nets),
            "symbols": sorted({r["symbol"] for r in arr}),
            "net_sum": round(sum(nets), 2),
            "avg_net": round(sum(nets) / len(nets), 2) if nets else None,
            "win_rate": round(sum(1 for v in nets if v > 0) / len(nets), 3) if nets else None,
        }

    return {
        "since": since,
        "all": summarize(rows),
        "low_price_lt50": summarize(low),
        "high_price_ge50": summarize(high),
    }


def build_report(rows: list[dict], db_path: Path, since: str = SINCE) -> dict:
    m = compute_metrics(rows)
    actual = compute_actual_trades(db_path, since)
    shadow_pairs = {(r.get("trade_date"), x.get("symbol"))
                    for r in rows for x in (r.get("shadow_execution_pool") or [])}
    # 交集按 (trade_date, symbol) 从 actual 明细重建 (compute_actual_trades 不返回明细)
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    ar = con.execute(
        "SELECT symbol, trade_date FROM trade_records WHERE source_type='PLAYBOOK_SIM' "
        "AND exit_price>0 AND trade_date>=? AND entry_session IN ('premarket','afterhours')",
        (since,),
    ).fetchall()
    con.close()
    actual_pairs = {(r["trade_date"], r["symbol"]) for r in ar}
    intersection = sorted(shadow_pairs & actual_pairs)

    window_ok = m["distinct_days"] >= MIN_DAYS and m["rows"] >= MIN_ROWS
    low_n = actual["low_price_lt50"]["n"]
    verdict = "WINDOW_MET_READOUT" if window_ok else "INSUFFICIENT_WINDOW"
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": str(DEFAULT_JSONL),
        "metrics": m,
        "actual_trades": actual,
        "shadow_actual_intersection": {
            "pairs": [f"{d} {s}" for d, s in intersection],
            "n": len(intersection),
        },
        "criteria": {
            "window": {"days": m["distinct_days"], "rows": m["rows"],
                       "threshold_days": MIN_DAYS, "threshold_rows": MIN_ROWS, "pass": window_ok},
            "low_price_actual_trades": {"n": low_n, "note": "判据③: entry_price<$50 的实际成交"},
            "intersection": {"n": len(intersection), "note": "判据④: 影子价池与实际成交 (date,symbol) 交集"},
        },
        "verdict": verdict,
        "notes": "非绩效观测; 不自动改 EXT_ENTRY_MIN_PRICE; 维持/退役由用户裁. "
                 f"$50 门槛中位移除当前价格池的 "
                 f"{m['shrink_ratio']['median']:.1%}; 实际低价成交 {low_n} 笔."
                 if m["shrink_ratio"]["median"] is not None else
                 "非绩效观测; 不自动改 EXT_ENTRY_MIN_PRICE; 维持/退役由用户裁.",
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ext_entry_exec_price 只读读数")
    ap.add_argument("--jsonl", default=str(DEFAULT_JSONL))
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--since", default=SINCE)
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args(argv)

    rows = load_rows(Path(args.jsonl))
    report = build_report(rows, Path(args.db), args.since)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    m, a = report["metrics"], report["actual_trades"]["all"]
    print(f"ext_exec_price: rows={m['rows']} days={m['distinct_days']} "
          f"shrink_median={m['shrink_ratio']['median']} low_trades={report['criteria']['low_price_actual_trades']['n']} "
          f"ext_trades={a['n']} verdict={report['verdict']} -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
