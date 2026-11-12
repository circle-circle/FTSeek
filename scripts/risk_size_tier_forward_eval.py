#!/usr/bin/env python3
"""RFC-155 P2 前瞻记录影子读数: risk_size_tier_forward.jsonl + trade_records.

只读 join, 复用 scripts/risk_size_tier_shadow.py 的美元口径指标.
旧六臂样本门: >=10 ET 日且 >=30 笔成功执行; 未达只报进度.
R2 open15 独立确认窗自 2026-11-11 起, 只裁决 open15, 旧样本不得凑数.
不自动改生产; 转生产须 RFC + 用户批准.
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from math import sqrt
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import risk_size_tier_shadow as hist  # noqa: E402

FORWARD = REPO / "data" / "shadow" / "risk_size_tier_forward.jsonl"
DB = REPO / "data" / "ftseek_us.db"
OUT = REPO / "data" / "shadow" / "risk_size_tier_forward_latest.json"
MIN_N = 30
MIN_DAYS = 10
R2_CONFIRM_START = "2026-11-11"
R2_DECISION_DATE = "2026-11-27"
R2_RULE = "open15"
Z80 = 1.2815515655446004
Z_POWER80 = 0.8416212335729143

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
            "id": row.get("inst_id"), "decision_id": str(row.get("decision_id") or ""),
            "symbol": row.get("symbol"),
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


def _r2_daily_delta(rows: list[dict]) -> dict[str, float]:
    daily = defaultdict(float)
    for row in rows:
        multiplier = hist.multiplier(R2_RULE, hist.flags_for(row))
        daily[row["trade_date"]] += row["pnl"] * (multiplier - 1.0)
    return {day: round(daily[day], 2) for day in sorted(daily)}


def _day_cluster(daily_delta: dict[str, float]) -> dict:
    values = list(daily_delta.values())
    if not values:
        return {"n_days": 0, "mean_delta": None, "ci80": None, "mde80": None}
    mean = statistics.mean(values)
    if len(values) < 2:
        return {"n_days": 1, "mean_delta": round(mean, 4), "ci80": None, "mde80": None}
    standard_error = statistics.stdev(values) / sqrt(len(values))
    return {
        "n_days": len(values),
        "mean_delta": round(mean, 4),
        "ci80": [round(mean - Z80 * standard_error, 4), round(mean + Z80 * standard_error, 4)],
        "mde80": round((Z80 + Z_POWER80) * standard_error, 4),
    }


def _r2_resolution_coverage(forward_rows: list[dict], outcomes: dict[str, dict]) -> dict:
    eligible = set()
    for row in forward_rows:
        if str(row.get("source") or "").upper() != "DA":
            continue
        decision_id = str(row.get("decision_id") or "")
        outcome = outcomes.get(decision_id) or {}
        trade_date = str(outcome.get("trade_date") or row.get("date") or "")
        if decision_id and R2_CONFIRM_START <= trade_date <= R2_DECISION_DATE:
            eligible.add(decision_id)
    resolved = {decision_id for decision_id in eligible if decision_id in outcomes}
    pending = sorted(eligible - resolved)
    return {
        "eligible_n": len(eligible),
        "resolved_n": len(resolved),
        "pending_n": len(pending),
        "pending_decision_ids": pending,
        "rate": round(len(resolved) / len(eligible), 4) if eligible else None,
    }


def compute_r2_open15_confirmation(rows: list[dict], resolution_coverage: float | None) -> dict:
    """R2-only 冻结窗判分；输入为 build_dataset 已结算行，不写文件、不改交易。"""
    selected = [
        r for r in rows
        if r["source"] == "DA" and R2_CONFIRM_START <= r["trade_date"] <= R2_DECISION_DATE
    ]
    report = hist.compute(selected)
    block = report["rules"][R2_RULE]
    daily_delta = _r2_daily_delta(selected)
    cluster = _day_cluster(daily_delta)
    best_day = max(daily_delta, key=daily_delta.get) if daily_delta else None
    without_best = [r for r in selected if r["trade_date"] != best_day] if best_day else []
    delta_without_best = hist.metrics(without_best, R2_RULE)["delta_net"] if best_day else None
    first_half_delta = report["halves"]["first_half"][R2_RULE]
    second_half_delta = report["halves"]["second_half"][R2_RULE]
    criteria = {
        "sample_affected_n>=30": block["affected_n"] >= MIN_N,
        "sample_days>=10": report["sample"]["distinct_days"] >= MIN_DAYS,
        "resolution_coverage>=0.95": resolution_coverage is not None and resolution_coverage >= 0.95,
        "delta_net>0": block["delta_net"] > 0,
        "drawdown_not_worse": block["cf_max_drawdown"] <= block["actual_max_drawdown"],
        "left_tail_improved": block["left_tail_cf_net"] > block["left_tail_actual_net"],
        "loss_avoided>upside_given_up": block["loss_avoided"] > block["upside_given_up"],
        "both_halves_nonnegative": first_half_delta >= 0 and second_half_delta >= 0,
        "excluding_best_day>0": delta_without_best is not None and delta_without_best > 0,
        "day_cluster_ci80_lower>0": bool(cluster["ci80"] and cluster["ci80"][0] > 0),
        "day_cluster_mean>mde80": bool(
            cluster["mde80"] is not None and cluster["mean_delta"] > cluster["mde80"]),
    }
    sample_ok = criteria["sample_affected_n>=30"] and criteria["sample_days>=10"]
    power_keys = {"day_cluster_ci80_lower>0", "day_cluster_mean>mde80"}
    if not sample_ok:
        verdict = "TOO_EARLY"
    elif all(criteria.values()):
        verdict = "R2_PROMOTE_CANDIDATE_REQUIRES_RFC_AND_USER_APPROVAL"
    elif all(value for key, value in criteria.items() if key not in power_keys):
        verdict = "INCONCLUSIVE_NO_DEPLOY"
    else:
        verdict = "NO_DEPLOY"
    return {
        "rule": R2_RULE,
        "source": "DA",
        "multiplier": 0.5,
        "cohort_start": R2_CONFIRM_START,
        "decision_date": R2_DECISION_DATE,
        "selection_note": "2026-11-11 前六臂样本只用于出题；本窗只裁决 open15，不挑其他臂。",
        "sample": report["sample"],
        "resolution_coverage": resolution_coverage,
        "affected_n": block["affected_n"],
        "actual_net": block["actual_net"],
        "cf_net": block["cf_net"],
        "delta_net": block["delta_net"],
        "actual_max_drawdown": block["actual_max_drawdown"],
        "cf_max_drawdown": block["cf_max_drawdown"],
        "left_tail_actual_net": block["left_tail_actual_net"],
        "left_tail_cf_net": block["left_tail_cf_net"],
        "loss_avoided": block["loss_avoided"],
        "upside_given_up": block["upside_given_up"],
        "first_half_delta": first_half_delta,
        "second_half_delta": second_half_delta,
        "daily_delta": daily_delta,
        "day_cluster": cluster,
        "best_contributing_day": best_day,
        "delta_excluding_best_day": delta_without_best,
        "criteria": criteria,
        "verdict": verdict,
        "automatic_trading_change": False,
    }


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
    r2_resolution = _r2_resolution_coverage(forward_rows, outcomes)
    report["r2_open15_confirmation"] = compute_r2_open15_confirmation(
        rows, resolution_coverage=r2_resolution["rate"])
    report["r2_open15_confirmation"]["resolution"] = r2_resolution
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
    print(f"OLD SAMPLE GATE (非毕业): {'PASS' if report['gate']['pass'] else 'NOT YET'} "
          f"{report['gate']}")
    r2 = report["r2_open15_confirmation"]
    print(f"R2 CONFIRM {r2['cohort_start']}→{r2['decision_date']} | "
          f"affected={r2['affected_n']}/{MIN_N} days={r2['sample']['distinct_days']}/{MIN_DAYS} "
          f"resolved={r2['resolution']['resolved_n']}/{r2['resolution']['eligible_n']} "
          f"delta={r2['delta_net']} exBest={r2['delta_excluding_best_day']} "
          f"cluster={r2['day_cluster']}")
    print(f"R2 VERDICT: {r2['verdict']} | criteria={r2['criteria']}")
    print("R2 通过也只允许另立 RFC 呈用户裁决；本脚本永不自动改交易。")
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
