#!/usr/bin/env python3
"""ext_entry_earnings_criterion_shadow 只读读数 (2026-10-18).

预注册: RFC-097 §预注册判据:
  ① 自生效日 (2026-09-23) 起攒 ≥20 笔延长时段成交 ⇒ 净均值≥0 维持, <0 或长期不足 ⇒ 回滚;
  ② legacy_blocked_new_pass 日去重计数, 回答"修口径放回多少票", 并与 §3.3 温和票交叉验证.

本脚本分开报两条轴, 不把台账行数当成交样本:
- 台账轴: JSONL rows / et_date 去重 / legacy_blocked_new_pass / legacy_reason / surprise;
- 成交轴: trade_records entry_session in (premarket, afterhours) since 2026-09-23;
- 判据②的人工交叉验证仍留给人; verdict 只给 SAMPLE/ROLLBACK/MAINTAIN 读数.

只读: JSONL 只读 + SQLite mode=ro; 只写 out JSON.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
from collections import Counter
import logging
from pathlib import Path

from app.utils.timezone import now_et

REPO = Path(__file__).resolve().parents[1]

logger = logging.getLogger(__name__)
DEFAULT_JSONL = REPO / "data" / "ext_entry_earnings_criterion_shadow.jsonl"
DEFAULT_DB = REPO / "data" / "ftseek_us.db"
DEFAULT_OUT = REPO / "data" / "shadow" / "ext_earnings_criterion_eval_latest.json"
EFFECTIVE_SINCE = "2026-09-23"
MIN_EXT_TRADES = 20


def load_rows(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as e:
                logger.warning("jsonl 坏行跳过: %s", e, exc_info=True)
                continue
    return rows


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * q))]


def compute_ledger(rows: list[dict]) -> dict:
    days = sorted({r.get("et_date") for r in rows if r.get("et_date")})
    blocked = [r for r in rows if r.get("legacy_blocked_new_pass") is True]
    blocked_days = sorted({r.get("et_date") for r in blocked if r.get("et_date")})
    per_day = Counter(r.get("et_date") for r in blocked if r.get("et_date"))
    surprises = [float(r["new_surprise_pct"]) for r in blocked
                 if isinstance(r.get("new_surprise_pct"), (int, float))]
    return {
        "rows": len(rows),
        "distinct_et_days": len(days),
        "first_day": days[0] if days else None,
        "last_day": days[-1] if days else None,
        "distinct_symbols": len({r.get("symbol") for r in rows if r.get("symbol")}),
        "legacy_blocked_new_pass_rows": len(blocked),
        "legacy_blocked_new_pass_days": len(blocked_days),
        "legacy_blocked_new_pass_per_day": dict(sorted(per_day.items())),
        "legacy_reason": dict(Counter(r.get("legacy_reason") for r in blocked)),
        "new_reason": dict(Counter(r.get("new_reason") for r in blocked)),
        "surprise_pct": {
            "n": len(surprises),
            "median": round(_pct(surprises, 0.50), 4) if surprises else None,
            "p90": round(_pct(surprises, 0.90), 4) if surprises else None,
            "max": round(max(surprises), 4) if surprises else None,
        },
    }


def compute_trades(db_path: Path, since: str = EFFECTIVE_SINCE) -> dict:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, symbol, entry_session, entry_price, quantity, "
        "COALESCE(pnl_net, realized_pnl, 0) AS net, metadata_json "
        "FROM trade_records WHERE source_type='PLAYBOOK_SIM' AND exit_price>0 "
        "AND trade_date>=? AND entry_session IN ('premarket','afterhours')",
        (since,),
    ).fetchall()
    con.close()

    nets = [float(r["net"] or 0.0) for r in rows]
    bps = []
    mild = []
    for r in rows:
        notional = float(r["entry_price"] or 0) * float(r["quantity"] or 0)
        if notional > 0:
            bps.append(float(r["net"] or 0.0) / notional * 10000)
        try:
            md = json.loads(r["metadata_json"] or "{}")
        except json.JSONDecodeError as e:
            logger.warning("metadata JSON 坏行: %s", e, exc_info=True)
            md = {}
        ch = md.get("ext_change_pct")
        if isinstance(ch, (int, float)) and 0.5 <= abs(ch) <= 2.0:
            mild.append(float(r["net"] or 0.0))

    def summarize(vals: list[float]) -> dict:
        return {
            "n": len(vals),
            "net_sum": round(sum(vals), 2),
            "avg_net": round(sum(vals) / len(vals), 2) if vals else None,
            "win_rate": round(sum(1 for v in vals if v > 0) / len(vals), 3) if vals else None,
        }

    return {
        "since": since,
        "ext_trades": summarize(nets),
        "net_bps": round(sum(bps) / len(bps), 2) if bps else None,
        "mild_cohort_0.5_2pct": summarize(mild),
        "caveat": "mild cohort 是事后按 metadata ext_change_pct 近似; §3.3 交叉验证仍须人工复核",
    }


def build_report(ledger_rows: list[dict], db_path: Path, since: str = EFFECTIVE_SINCE) -> dict:
    ledger = compute_ledger(ledger_rows)
    trades = compute_trades(db_path, since)
    n = trades["ext_trades"]["n"]
    avg = trades["ext_trades"]["avg_net"]
    if n < MIN_EXT_TRADES:
        verdict = "INSUFFICIENT_EXT_TRADES"
    elif avg is not None and avg < 0:
        verdict = "ROLLBACK_CANDIDATE"
    else:
        verdict = "MAINTAIN_CANDIDATE_MANUAL_CROSSCHECK"
    return {
        "generated_at": now_et().isoformat(timespec="seconds"),
        "source": str(DEFAULT_JSONL),
        "ledger": ledger,
        "trades": trades,
        "criteria": {
            "ext_trade_sample": {"n": n, "threshold": MIN_EXT_TRADES,
                                 "pass": n >= MIN_EXT_TRADES},
            "ext_trade_net": {"avg_net": avg,
                              "pass": None if n < MIN_EXT_TRADES else bool(avg is not None and avg >= 0)},
            "ledger_crosscheck": {"legacy_blocked_new_pass_days": ledger["legacy_blocked_new_pass_days"],
                                  "note": "判据②是人工交叉验证轴; 不自动裁决"},
        },
        "verdict": verdict,
        "notes": "只读读数; 不自动改 .env.us / 不改入场阈值. 台账行数与成交样本是两条轴.",
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ext earnings criterion 只读读数")
    ap.add_argument("--jsonl", default=str(DEFAULT_JSONL))
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--since", default=EFFECTIVE_SINCE)
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args(argv)

    rows = load_rows(Path(args.jsonl))
    report = build_report(rows, Path(args.db), args.since)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    l, t = report["ledger"], report["trades"]["ext_trades"]
    print(f"ext_earnings: ledger_rows={l['rows']} days={l['distinct_et_days']} "
          f"blocked_new_pass={l['legacy_blocked_new_pass_rows']} "
          f"ext_trades={t['n']} avg=${t['avg_net']} verdict={report['verdict']} -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
