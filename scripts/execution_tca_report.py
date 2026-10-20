#!/usr/bin/env python3
"""RFC-152 Phase 0 只读报表: 执行 TCA + 点差/报价年龄门反事实。

读数口径 (预注册见 docs/decisions/RFC-152-execution-tca-spread-shadow.md §5):
  - 样本门: >=10 个 ET 交易日 且 >=30 笔 success=True 成交行; 未达门只报进度.
  - 必报: 点差/报价年龄分布; 计划锚->重定价锚->成交价的实现缺口;
          点差/报价年龄分桶 x 扣费净 bps + 按日聚类 t + MDE80 + 误杀计数.
  - provisional_would_block 只是 jsonl 标签, 不是生效门; 本脚本不改变任何生产行为.

只读: JSONL 只读 + SQLite mode=ro; 只写 out JSON.
用法: PYTHONPATH=. venv/bin/python scripts/execution_tca_report.py
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_JSONL = REPO / "data" / "shadow" / "execution_tca.jsonl"
DEFAULT_DB = REPO / "data" / "ftseek_us.db"
DEFAULT_OUT = REPO / "data" / "shadow" / "execution_tca_latest.json"

SINCE = "2026-10-20"
MIN_DAYS = 10
MIN_FILLED = 30
MDE_Z = 2.802
SPREAD_BUCKETS = ((0.0, 5.0, "[0,5)"), (5.0, 10.0, "[5,10)"),
                  (10.0, 20.0, "[10,20)"), (20.0, 50.0, "[20,50)"),
                  (50.0, float("inf"), ">=50"))

logger = logging.getLogger(__name__)


def load_rows(path: Path, since: str) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    with path.open(encoding="utf-8") as source:
        for line in source:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                logger.warning("jsonl 坏行跳过: %s", error, exc_info=True)
                continue
            if str(row.get("date") or "") < since:
                continue
            rows.append(row)
    return rows


def _q(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * p))]


def _day_clustered(values_by_day: dict[str, list[float]]) -> dict | None:
    """日聚类 t + MDE80; n_days<3 或零方差 -> None (不可判)."""
    daily = [statistics.mean(v) for v in values_by_day.values() if v]
    if len(daily) < 3:
        return None
    sd = statistics.stdev(daily)
    if sd == 0:
        return None
    mean = statistics.mean(daily)
    t_stat = mean / (sd / math.sqrt(len(daily)))
    return {
        "n_days": len(daily),
        "day_mean_bps": round(mean, 2),
        "t": round(t_stat, 2),
        "mde_80pct": round(MDE_Z * sd / math.sqrt(len(daily)), 2),
        "distinguishable": bool(abs(t_stat) >= 2.0),
    }


def _stat_block(pairs: list[tuple[str, float]]) -> dict:
    """pairs = [(ET date, net bps), ...]."""
    values = [v for _, v in pairs]
    by_day: dict[str, list[float]] = defaultdict(list)
    for day, value in pairs:
        by_day[day].append(value)
    return {
        "n": len(values),
        "distinct_days": len(by_day),
        "mean_bps": round(statistics.mean(values), 2) if values else None,
        "median_bps": round(statistics.median(values), 2) if values else None,
        "win_rate_pct": (round(100.0 * sum(1 for v in values if v > 0) / len(values), 2)
                         if values else None),
        "positive_n": sum(1 for v in values if v > 0),
        "day_clustered": _day_clustered(by_day),
    }


def _spread_bucket(value: float | None) -> str:
    if value is None:
        return "missing"
    for low, high, label in SPREAD_BUCKETS:
        if low <= value < high:
            return label
    return ">=50"


def _age_bucket(value: float | None) -> str:
    if value is None:
        return "missing"
    if value < 500:
        return "<500ms"
    if value < 2000:
        return "500-2000ms"
    return ">=2000ms"


def load_outcomes(db_path: Path, decision_ids: set[str]) -> dict[str, dict]:
    """只读 join trade_records; 同一 decision_id 多笔聚合 (sum pnl / sum notional)."""
    if not decision_ids or not db_path.exists():
        return {}
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10.0)
    connection.row_factory = sqlite3.Row
    try:
        rows: list[sqlite3.Row] = []
        ids = [value for value in decision_ids if value]
        for index in range(0, len(ids), 500):
            chunk = ids[index:index + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows.extend(connection.execute(
                "SELECT decision_id, trade_date, symbol, direction, "
                "COALESCE(pnl_net, realized_pnl) AS pnl, entry_price, quantity, "
                "exit_reason FROM trade_records "
                f"WHERE decision_id IN ({placeholders})",
                chunk,
            ).fetchall())
    finally:
        connection.close()

    grouped: dict[str, dict] = {}
    for row in rows:
        decision_id = str(row["decision_id"] or "")
        if not decision_id:
            continue
        pnl = row["pnl"]
        entry = row["entry_price"]
        quantity = row["quantity"]
        if pnl is None or entry is None or quantity is None:
            continue
        notional = float(entry) * float(quantity)
        if notional <= 0:
            continue
        bucket = grouped.setdefault(decision_id, {
            "pnl": 0.0, "notional": 0.0, "trade_date": row["trade_date"],
            "exit_reason": row["exit_reason"],
        })
        bucket["pnl"] += float(pnl)
        bucket["notional"] += notional

    return {
        decision_id: {
            "net_bps": round(item["pnl"] / item["notional"] * 10000.0, 4),
            "pnl": round(item["pnl"], 2),
            "trade_date": item["trade_date"],
            "exit_reason": item["exit_reason"],
        }
        for decision_id, item in grouped.items()
    }


def _dist(values: list[float]) -> dict:
    return {
        "n": len(values),
        "p50": round(_q(values, 0.50), 2) if values else None,
        "p80": round(_q(values, 0.80), 2) if values else None,
        "p90": round(_q(values, 0.90), 2) if values else None,
        "max": round(max(values), 2) if values else None,
        "mean": round(statistics.mean(values), 2) if values else None,
    }


def _group_shortfall(rows: list[dict], field: str) -> dict:
    out: dict[str, dict] = {}
    keys = sorted({str(r.get(field) if r.get(field) is not None else "missing")
                   for r in rows})
    for key in keys:
        subset = [r for r in rows
                  if str(r.get(field) if r.get(field) is not None else "missing") == key]
        out[key] = {
            "n": len(subset),
            "planned_to_exec_bps_mean": (_mean_present(subset, "planned_to_exec_bps")),
            "exec_to_fill_bps_mean": (_mean_present(subset, "exec_to_fill_bps")),
            "planned_to_fill_bps_mean": (_mean_present(subset, "planned_to_fill_bps")),
        }
    return out


def _mean_present(rows: list[dict], key: str) -> float | None:
    values = [float(r[key]) for r in rows
              if isinstance(r.get(key), (int, float))]
    return round(statistics.mean(values), 2) if values else None


def compute(rows: list[dict], outcomes: dict[str, dict]) -> dict:
    filled = [r for r in rows if r.get("success") is True and float(r.get("fill_price") or 0) > 0]
    days = sorted({str(r.get("date")) for r in filled if r.get("date")})
    joined = [(r, outcomes.get(str(r.get("decision_id") or ""))) for r in filled]
    paired = [(r, o) for r, o in joined if o is not None]

    spread_values = [float(r["spread_bps"]) for r in filled
                     if isinstance(r.get("spread_bps"), (int, float))]
    age_values = [float(r["quote_age_ms"]) for r in filled
                  if isinstance(r.get("quote_age_ms"), (int, float))]

    spread_buckets: dict[str, list[tuple[str, float]]] = defaultdict(list)
    age_buckets: dict[str, list[tuple[str, float]]] = defaultdict(list)
    gate_buckets: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for row, outcome in paired:
        day = str(outcome.get("trade_date") or row.get("date") or "")
        net_bps = float(outcome["net_bps"])
        spread_buckets[_spread_bucket(row.get("spread_bps"))].append((day, net_bps))
        age_buckets[_age_bucket(row.get("quote_age_ms"))].append((day, net_bps))
        gate_buckets["would_block" if row.get("provisional_would_block") else "pass"].append(
            (day, net_bps))

    return {
        "schema": "execution_tca_report_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "window": {"since": SINCE, "first_day": days[0] if days else None,
                   "last_day": days[-1] if days else None},
        "sample": {
            "attempts_n": len(rows),
            "filled_n": len(filled),
            "fill_rate_pct": round(100.0 * len(filled) / len(rows), 2) if rows else None,
            "distinct_days": len(days),
            "joined_n": len(paired),
            "unjoined_filled_n": len(filled) - len(paired),
            "book_proxy_n": sum(1 for r in filled if r.get("book_is_proxy")),
            "plan_anchor_missing_n": sum(1 for r in filled
                                         if r.get("planned_entry_source") == "missing"),
            "gate_passed": len(days) >= MIN_DAYS and len(filled) >= MIN_FILLED,
            "gate_criteria": f">={MIN_DAYS} ET days AND >={MIN_FILLED} filled rows",
        },
        "spread_bps": _dist(spread_values),
        "quote_age_ms": _dist(age_values),
        "implementation_shortfall": {
            "planned_to_exec_bps": _shortfall_block(filled, "planned_to_exec_bps"),
            "exec_to_fill_bps": _shortfall_block(filled, "exec_to_fill_bps"),
            "planned_to_fill_bps": _shortfall_block(filled, "planned_to_fill_bps"),
            "by_source": _group_shortfall(filled, "source"),
            "by_archetype": _group_shortfall(filled, "entry_archetype"),
        },
        "spread_buckets": {key: _stat_block(value)
                           for key, value in sorted(spread_buckets.items())},
        "quote_age_buckets": {key: _stat_block(value)
                              for key, value in sorted(age_buckets.items())},
        "provisional_gate": {key: _stat_block(value)
                             for key, value in sorted(gate_buckets.items())},
        "caveats": [
            "SIM 成交不可直接外推实盘 (点差/滑点关系可迁移, 绝对值不可)。",
            "book proxy 行 spread_bps=None, 单列 missing, 不混入点差桶。",
            "只覆盖已过全部既有闸门的执行尝试, 有选择偏差; 不回答更早候选被门拦下是否更差。",
            "provisional_would_block 只是标签, 不是生效门; 转生产须另立 RFC + 用户批准。",
            "n<30 或 <10 个交易日只报进度, 不作裁决 (宪章第 5 律)。",
        ],
    }


def _shortfall_block(rows: list[dict], key: str) -> dict:
    values = [float(r[key]) for r in rows if isinstance(r.get(key), (int, float))]
    if not values:
        return {"n": 0, "mean_bps": None, "median_bps": None}
    return {
        "n": len(values),
        "mean_bps": round(statistics.mean(values), 2),
        "median_bps": round(statistics.median(values), 2),
    }


def render_console(report: dict) -> None:
    sample = report["sample"]
    print("=" * 72)
    print("执行 TCA 只读报表 (RFC-152 Phase 0)")
    print(f"窗口: {report['window']['first_day']} → {report['window']['last_day']} "
          f"| 尝试 {sample['attempts_n']} / 成交 {sample['filled_n']} "
          f"| 交易日 {sample['distinct_days']} | join {sample['joined_n']}")
    print(f"样本门: {'PASS' if sample['gate_passed'] else 'NOT YET'} "
          f"({sample['gate_criteria']})")
    print(f"点差 bps: p50={report['spread_bps']['p50']} p80={report['spread_bps']['p80']} "
          f"p90={report['spread_bps']['p90']} (n={report['spread_bps']['n']})")
    print(f"报价年龄 ms: p50={report['quote_age_ms']['p50']} "
          f"p90={report['quote_age_ms']['p90']} (n={report['quote_age_ms']['n']})")
    short = report["implementation_shortfall"]
    print(f"计划→重定价: {short['planned_to_exec_bps']} bps")
    print(f"重定价→成交: {short['exec_to_fill_bps']} bps")
    print(f"计划→成交: {short['planned_to_fill_bps']} bps")
    for label, key in (("点差桶", "spread_buckets"), ("年龄桶", "quote_age_buckets"),
                       ("临时门标签", "provisional_gate")):
        print(f"-- {label} --")
        for bucket, block in report[key].items():
            clustered = block.get("day_clustered") or {}
            print(f"  {bucket}: n={block['n']} mean={block['mean_bps']}bps "
                  f"median={block['median_bps']} win={block['win_rate_pct']}% "
                  f"t={clustered.get('t')} MDE80={clustered.get('mde_80pct')}")
    print("=" * 72)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jsonl", default=str(DEFAULT_JSONL))
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--since", default=SINCE)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    path = Path(args.jsonl)
    if not path.exists():
        print(f"JSONL 不存在 (尚未产生执行尝试): {path}")
        rows: list[dict] = []
    else:
        rows = load_rows(path, args.since)
    decision_ids = {str(r.get("decision_id") or "") for r in rows}
    outcomes = load_outcomes(Path(args.db), decision_ids)
    report = compute(rows, outcomes)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    render_console(report)
    print(f"已写入: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
