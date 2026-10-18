#!/usr/bin/env python3
"""entry_anchor_dual 阶段0 只读读数 (2026-10-18).

预注册: RFC-123 §预注册判据 (见 shadow_registry entry_anchor_dual):
  影子期≥10交易日 ∧ ①生成锚缺失率<1% ②双锚触发分歧率≥5% ③分歧样本按生成锚
  判定的触发质量(含费PnL)不低于现行锚 ④现行护栏触顶频率无异常上升.

本脚本只做读数, 不裁决:
- ①②④ 可从 entry_timing_shadow.jsonl 直接算;
- ③ 需要"生成锚反事实"的成交/退出 PnL — 当前行只有 gen_triggered/gen_path,
  没有生成锚的成交结果 ⇒ 标记 NOT_COMPUTABLE_COUNTERFACTUAL, 只用实际
  live-anchor 成交做代理读数并标注, 不冒充判据.
- 未达 10 交易日 → verdict=NOT_READY_PERIOD, 不做任何毕业/转正建议.

只读: JSONL 只读 + SQLite mode=ro; 只写 out JSON.
用法: PYTHONPATH=. venv/bin/python scripts/entry_anchor_dual_eval.py
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_JSONL = REPO / "data" / "entry_timing_shadow.jsonl"
DEFAULT_DB = REPO / "data" / "ftseek_us.db"
DEFAULT_OUT = REPO / "data" / "shadow" / "entry_anchor_dual_eval_latest.json"
MIN_DAYS = 10
DIVERGENCE_THRESHOLD_PCT = 5.0


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


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * q))]


def compute_metrics(rows: list[dict]) -> dict:
    anchor = [r for r in rows if r.get("anchor_recorded") is True]
    days = sorted({(r.get("ts") or "")[:10] for r in anchor if r.get("ts")})
    inst_ids = {r.get("inst_id") for r in anchor if r.get("inst_id") is not None}
    dual = [r.get("dual_anchor") for r in anchor if isinstance(r.get("dual_anchor"), dict)]

    gen_sources = Counter((d or {}).get("gen_source") or "missing" for d in dual)
    missing = gen_sources.get("missing", 0)
    missing_rate = missing / len(anchor) if anchor else 0.0

    divergences = [float(d["divergence_pct"]) for d in dual
                   if isinstance(d.get("divergence_pct"), (int, float))]
    div_rows = [r for r in anchor
                if isinstance(r.get("dual_anchor"), dict)
                and r["dual_anchor"].get("trigger_divergent") is True]
    non_div_rows = [r for r in anchor
                    if isinstance(r.get("dual_anchor"), dict)
                    and r["dual_anchor"].get("trigger_divergent") is False]
    trigger_matrix = Counter(
        (bool((r.get("dual_anchor") or {}).get("gen_triggered")),
         bool((r.get("dual_anchor") or {}).get("live_triggered")))
        for r in anchor if isinstance(r.get("dual_anchor"), dict))

    def gate_rate(grp: list[dict]) -> float | None:
        return round(sum(1 for r in grp if r.get("gate_block")) / len(grp), 4) if grp else None

    return {
        "total_rows": len(rows),
        "anchor_rows": len(anchor),
        "distinct_inst_ids": len(inst_ids),
        "distinct_days": len(days),
        "first_day": days[0] if days else None,
        "last_day": days[-1] if days else None,
        "missing": missing,
        "missing_rate": round(missing_rate, 4),
        "gen_source": dict(gen_sources),
        "divergence": {
            "n": len(divergences),
            "median_pct": round(_pct(divergences, 0.50), 4) if divergences else None,
            "p90_pct": round(_pct(divergences, 0.90), 4) if divergences else None,
            "p95_pct": round(_pct(divergences, 0.95), 4) if divergences else None,
            "ge_5pct": sum(1 for x in divergences if x >= DIVERGENCE_THRESHOLD_PCT),
            "ge_5pct_rate": round(sum(1 for x in divergences if x >= DIVERGENCE_THRESHOLD_PCT)
                                  / len(divergences), 4) if divergences else 0.0,
        },
        "trigger_divergent": len(div_rows),
        "trigger_divergent_rate": round(len(div_rows) / len(anchor), 4) if anchor else 0.0,
        "trigger_matrix": {f"gen={g},live={l}": n for (g, l), n in sorted(trigger_matrix.items())},
        "gate_block": {
            "all_rate": gate_rate(anchor),
            "divergent_rate": gate_rate(div_rows),
            "non_divergent_rate": gate_rate(non_div_rows),
            "n_all": len(anchor),
            "n_divergent": len(div_rows),
            "n_non_divergent": len(non_div_rows),
        },
    }


def join_outcomes(rows: list[dict], db_path: Path) -> dict:
    """实际 live-anchor 成交代理读数 (非生成锚反事实, 只作参考)."""
    anchor = [r for r in rows if r.get("anchor_recorded") is True]
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    trades = con.execute(
        "SELECT id, metadata_json, COALESCE(pnl_net, realized_pnl, 0) AS net "
        "FROM trade_records WHERE source_type='PLAYBOOK_SIM' AND exit_price>0"
    ).fetchall()
    con.close()

    by_inst: dict[int, list[float]] = defaultdict(list)
    for t in trades:
        try:
            md = json.loads(t["metadata_json"] or "{}")
        except json.JSONDecodeError:
            md = {}
        pi = md.get("playbook_instruction_id")
        if pi is not None:
            by_inst[int(pi)].append(float(t["net"] or 0.0))

    per_inst: dict[int, dict] = defaultdict(lambda: {"div": False, "rows": 0, "gate": 0})
    for r in anchor:
        i = r.get("inst_id")
        if i is None:
            continue
        per_inst[int(i)]["rows"] += 1
        if r.get("gate_block"):
            per_inst[int(i)]["gate"] += 1
        if isinstance(r.get("dual_anchor"), dict) and r["dual_anchor"].get("trigger_divergent") is True:
            per_inst[int(i)]["div"] = True

    matched = []
    for i, v in per_inst.items():
        if i in by_inst:
            matched.append((v["div"], sum(by_inst[i])))

    def summarize(grp):
        vals = [net for _, net in grp]
        return {
            "n": len(vals),
            "net_sum": round(sum(vals), 2),
            "avg_net": round(sum(vals) / len(vals), 2) if vals else None,
            "win_rate": round(sum(1 for v in vals if v > 0) / len(vals), 3) if vals else None,
        }

    div = [m for m in matched if m[0]]
    non = [m for m in matched if not m[0]]
    return {
        "matched_inst_ids": len(matched),
        "divergent": summarize(div),
        "non_divergent": summarize(non),
        "caveat": "实际 live-anchor 成交 PnL 代理; 非生成锚反事实, 不满足预注册③的口径",
    }


def build_report(rows: list[dict], db_path: Path, min_days: int = MIN_DAYS) -> dict:
    m = compute_metrics(rows)
    outcome = join_outcomes(rows, db_path)
    period_ok = m["distinct_days"] >= min_days
    missing_ok = m["missing_rate"] < 0.01
    divergence_ok = m["trigger_divergent_rate"] >= 0.05
    if not period_ok:
        verdict = "NOT_READY_PERIOD"
    elif not missing_ok:
        verdict = "FAIL_MISSING_RATE"
    elif not divergence_ok:
        verdict = "FAIL_DIVERGENCE_RATE"
    else:
        verdict = "MANUAL_REVIEW"
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": str(DEFAULT_JSONL),
        "min_days": min_days,
        "metrics": m,
        "outcome_proxy": outcome,
        "criteria": {
            "period_days": {"value": m["distinct_days"], "threshold": min_days, "pass": period_ok},
            "missing_rate": {"value": m["missing_rate"], "threshold": 0.01, "pass": missing_ok},
            "divergence_rate": {"value": m["trigger_divergent_rate"],
                                "threshold": 0.05, "pass": divergence_ok},
            "quality": {"value": "NOT_COMPUTABLE_COUNTERFACTUAL",
                        "note": "行内只有 gen_triggered/gen_path, 无生成锚成交 PnL; 实际结局代理仅供诊断"},
            "guardrail": {"value": m["gate_block"]["all_rate"],
                          "note": "无同窗前向基线; 只报读数, 不判异常"},
        },
        "verdict": verdict,
        "notes": "只读读数; 不自动转 READY/毕业. 10 交易日后重跑; 质量腿需生成锚反事实回放.",
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="entry_anchor_dual 阶段0 只读读数")
    ap.add_argument("--jsonl", default=str(DEFAULT_JSONL))
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--min-days", type=int, default=MIN_DAYS)
    args = ap.parse_args(argv)

    rows = load_rows(Path(args.jsonl))
    report = build_report(rows, Path(args.db), args.min_days)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    m = report["metrics"]
    print(f"entry_anchor_dual: rows={m['total_rows']} anchor={m['anchor_rows']} "
          f"inst={m['distinct_inst_ids']} days={m['distinct_days']} "
          f"missing={m['missing_rate']:.2%} div={m['trigger_divergent_rate']:.2%} "
          f"gate={m['gate_block']['all_rate']:.2%}")
    print(f"verdict={report['verdict']} -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
