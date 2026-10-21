#!/usr/bin/env python3
"""RFC-155 P2 前瞻记录影子读数: risk_size_tier_forward.jsonl + trade_records.

只读 join, 复用 scripts/risk_size_tier_shadow.py 的美元口径指标.
样本门: >=10 ET 日且 >=30 笔成功执行; 未达只报进度.
不自动改生产; 转生产须 RFC + 用户批准.
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import risk_size_tier_shadow as hist  # noqa: E402

FORWARD = REPO / "data" / "shadow" / "risk_size_tier_forward.jsonl"
DB = REPO / "data" / "ftseek_us.db"
OUT = REPO / "data" / "shadow" / "risk_size_tier_forward_latest.json"
MIN_N = 30
MIN_DAYS = 10

logger = logging.getLogger(__name__)


def load_forward(path: Path) -> list[dict]:
    rows = []
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
                logger.warning("forward jsonl 坏行跳过: %s", error, exc_info=True)
                continue
            if row.get("schema") != "risk_size_tier_forward_v1":
                continue
            if row.get("success") is not True:
                continue
            if not row.get("actual_size") or not row.get("actual_notional"):
                continue
            rows.append(row)
    # 同一 decision_id 只取最后一行 (重试/多轮时保留最终成功)
    dedup: dict[str, dict] = {}
    for row in rows:
        key = str(row.get("decision_id") or row.get("inst_id") or "")
        dedup[key] = row
    return list(dedup.values())


def load_trade_outcomes(db_path: Path, decision_ids: set[str]) -> dict[str, dict]:
    if not decision_ids or not db_path.exists():
        return {}
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10.0)
    con.row_factory = sqlite3.Row
    try:
        ids = [x for x in decision_ids if x]
        rows = []
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            q = ",".join("?" for _ in chunk)
            rows.extend(con.execute(
                "SELECT decision_id,trade_date,pnl_net,exit_reason FROM trade_records "
                f"WHERE decision_id IN ({q}) AND exit_time IS NOT NULL AND exit_time<>''",
                chunk,
            ))
    finally:
        con.close()
    grouped: dict[str, dict] = {}
    for row in rows:
        key = str(row["decision_id"] or "")
        if not key:
            continue
        item = grouped.setdefault(key, {"pnl": 0.0, "trade_date": row["trade_date"],
                                        "exit_reason": row["exit_reason"]})
        item["pnl"] += float(row["pnl_net"] or 0)
    return grouped


def build_dataset(forward_rows: list[dict], outcomes: dict[str, dict]) -> tuple[list[dict], dict]:
    rows = []
    skipped = 0
    for row in forward_rows:
        outcome = outcomes.get(str(row.get("decision_id") or ""))
        if not outcome:
            skipped += 1
            continue
        rows.append({
            "id": row.get("inst_id"), "symbol": row.get("symbol"),
            "trade_date": str(outcome.get("trade_date") or row.get("date") or ""),
            "source": row.get("source") or "OTHER",
            "atr_pct": row.get("atr_pct"),
            "session_min": row.get("session_min"),
            "pnl": float(outcome.get("pnl") or 0),
            "notional": float(row.get("actual_notional") or 0),
            "left_tail": str(outcome.get("exit_reason") or "") in hist.STOP_LIKE,
        })
    return rows, {"forward_rows": len(forward_rows), "joined": len(rows),
                  "skipped_no_trade": skipped}


def compute(forward_rows: list[dict], outcomes: dict[str, dict]) -> dict:
    rows, coverage = build_dataset(forward_rows, outcomes)
    report = hist.compute(rows)
    days = report["sample"]["distinct_days"]
    # [2026-10-21 口径对齐] RFC-155 §3.2 原文 = 「≥30 笔**受影响**交易」, 旧实现拿
    #   sample.n (全部 join 到的成交) 当门 ⇒ 门被放松 (30 笔总单里可能只有十来笔真被缩放)。
    #   any 规则的 affected_n 才是「至少一个旗标为真 ⇒ mult<1」的笔数, 用它对齐原文。
    affected_n = (report["rules"].get("any") or {}).get("affected_n") or 0
    gate = {
        "sample_n": affected_n >= MIN_N,          # 判据 = 受影响笔数 (RFC-155 §3.2 原文)
        "sample_affected_n": affected_n,
        "sample_total_n_only_printed": report["sample"]["n"],   # 只印不判
        "sample_days": days >= MIN_DAYS,
        "note": ("样本门未达只报进度; 通过不自动改生产。判据=受影响笔数 (≥1 旗标为真), "
                 "非全部已执行笔数"),
    }
    gate["pass"] = bool(gate["sample_n"] and gate["sample_days"])
    report["coverage"] = coverage
    report["gate"] = gate
    report["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return report


def render(report: dict) -> None:
    cov = report["coverage"]
    print("=" * 78)
    print("RFC-155 P2 前瞻记录影子读数 (只读)")
    print(f"forward {cov['forward_rows']} / joined {cov['joined']} / "
          f"no-trade {cov['skipped_no_trade']} | n={report['sample']['n']} "
          f"days={report['sample']['distinct_days']} "
          f"受影响(判据口径)={report['gate']['sample_affected_n']}/{MIN_N}")
    for rule, block in report["rules"].items():
        print(f"  {rule:8s} aff={block['affected_n']:3d} actual={block['actual_net']:9.2f} "
              f"cf={block['cf_net']:9.2f} delta={block['delta_net']:9.2f} "
              f"DD {block['actual_max_drawdown']}->{block['cf_max_drawdown']}")
    print(f"GATE: {'PASS' if report['gate']['pass'] else 'NOT YET'} "
          f"{report['gate']}")
    print("=" * 78)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--forward", default=str(FORWARD))
    parser.add_argument("--db", default=str(DB))
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    rows = load_forward(Path(args.forward))
    outcomes = load_trade_outcomes(Path(args.db),
                                   {str(r.get("decision_id") or "") for r in rows})
    report = compute(rows, outcomes)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    render(report)
    print(f"已写入: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
