#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""UE/DA 链路契约健康检查 (只读, 2026-10-18).

回答:
  - 最新 UE scan 的 prompt/brief 版本、material stats、surfaced 方向
  - surfaced SHORT 的 borrow basis/feasible、event_meta 覆盖
  - borrow_on_demand 补查计数
  - 最新 DA instructions 的 score_band/evidence_gaps/UE 输入持久化覆盖

不写库、不调 LLM、不下单.
用法:
  PYTHONPATH=. ./venv/bin/python scripts/ue_da_chain_health.py
  ... --json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCAN_PATH = ROOT / "data" / "shadow" / "llm_universe_scan.jsonl"
DB_PATH = ROOT / "data" / "ftseek_us.db"


def _load_json(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def latest_scan(path: Path = SCAN_PATH) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    latest = None
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except (TypeError, ValueError):
            continue
        if record.get("type") == "scan":
            latest = record
    return latest


def summarize_scan(scan: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not scan:
        return {"scan_found": False}
    surfaced = scan.get("surfaced") or []
    directions = Counter(str(s.get("direction") or "UNKNOWN") for s in surfaced)
    short_basis = Counter()
    short_feasible = Counter()
    event_meta_coverage = 0
    for item in surfaced:
        if str(item.get("direction") or "") != "SHORT":
            continue
        sc = item.get("short_check") or {}
        short_basis[str(sc.get("borrow_data_basis") or "MISSING")] += 1
        short_feasible[str(sc.get("borrow_feasible"))] += 1
    for item in surfaced:
        if isinstance(item.get("evidence_matched_event_meta"), dict) and item.get("evidence_matched_event_meta"):
            event_meta_coverage += 1
    return {
        "scan_found": True,
        "date": scan.get("date"),
        "scanned_at": scan.get("scanned_at"),
        "window": scan.get("scan_window"),
        "batch_status": scan.get("batch_status"),
        "prompt_version": scan.get("prompt_version"),
        "brief_template_version": scan.get("brief_template_version"),
        "brief_material_stats": scan.get("brief_material_stats") or {},
        "surfaced_n": len(surfaced),
        "directions": dict(directions),
        "short_basis": dict(short_basis),
        "short_feasible": dict(short_feasible),
        "event_meta_coverage": event_meta_coverage,
        "borrow_on_demand": scan.get("borrow_on_demand") or {},
    }


def latest_da_rows(db_path: Path = DB_PATH, limit: int = 30) -> List[Dict[str, Any]]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT id, date, symbol, direction, decision_id, raw_instruction_json,
                  confidence, execution_state
           FROM playbook_instructions
           WHERE direction IN ('BUY', 'SELL_SHORT')
           ORDER BY id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def summarize_instruction(row: Dict[str, Any]) -> Dict[str, Any]:
    raw = _load_json(row.get("raw_instruction_json"))
    origin = raw.get("origin") if isinstance(raw.get("origin"), dict) else {}
    event_meta = origin.get("evidence_matched_event_meta")
    if not isinstance(event_meta, dict):
        event_meta = raw.get("ue_event_meta") if isinstance(raw.get("ue_event_meta"), dict) else {}
    short_check = raw.get("ue_short_check") if isinstance(raw.get("ue_short_check"), dict) else {}
    gaps = raw.get("evidence_gaps")
    return {
        "id": row.get("id"),
        "date": row.get("date"),
        "symbol": row.get("symbol"),
        "direction": row.get("direction"),
        "decision_id": row.get("decision_id"),
        "score_band": raw.get("score_band") or "UNKNOWN",
        "evidence_gaps_n": len(gaps) if isinstance(gaps, (list, dict)) else (1 if gaps else 0),
        "debate_outcome": str(raw.get("debate_outcome") or "")[:120],
        "ue_event_meta": bool(event_meta),
        "ue_event_family": event_meta.get("event_family") if isinstance(event_meta, dict) else None,
        "ue_borrow_basis": short_check.get("borrow_data_basis") or raw.get("ue_borrow_data_basis"),
        "ue_borrow_feasible": short_check.get("borrow_feasible"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="UE/DA chain contract health check")
    parser.add_argument("--scan-path", type=Path, default=SCAN_PATH)
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    scan = summarize_scan(latest_scan(args.scan_path))
    rows = [summarize_instruction(row)
            for row in latest_da_rows(args.db, args.limit)]
    da_score_bands = Counter(r["score_band"] for r in rows)
    da_event_meta = sum(1 for r in rows if r["ue_event_meta"])
    da_borrow_basis = Counter(str(r["ue_borrow_basis"] or "MISSING") for r in rows)
    da_gaps = sum(1 for r in rows if r["evidence_gaps_n"] > 0)
    payload = {
        "ue_scan": scan,
        "da": {
            "n": len(rows),
            "score_bands": dict(da_score_bands),
            "ue_event_meta_coverage": da_event_meta,
            "borrow_basis": dict(da_borrow_basis),
            "rows_with_evidence_gaps": da_gaps,
            "recent": rows[:10],
        },
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if not scan.get("scan_found"):
        print("UE scan: NOT FOUND")
    else:
        print(f"UE scan: {scan['date']} {scan['window']} prompt={scan['prompt_version']} "
              f"brief={scan['brief_template_version']} surfaced={scan['surfaced_n']} "
              f"dirs={scan['directions']}")
        print(f"  material_stats={scan['brief_material_stats']}")
        print(f"  short_basis={scan['short_basis']} feasible={scan['short_feasible']} "
              f"event_meta_coverage={scan['event_meta_coverage']}")
        print(f"  borrow_on_demand={scan['borrow_on_demand']}")
    print(f"DA: n={len(rows)} score_bands={dict(da_score_bands)} "
          f"ue_event_meta={da_event_meta}/{len(rows)} "
          f"borrow_basis={dict(da_borrow_basis)} gaps_rows={da_gaps}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
