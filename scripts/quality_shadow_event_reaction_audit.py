#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""quality_shadow event_reaction 落库验收 (只读, 2026-10-21).

用途: 在 quality_shadow 新代码重启后, 验收下一批 DA 指令是否把
event_age_h / post_event_move_pct / reaction_basis / reaction_source /
reaction_reason 写进 metadata_json.quality_shadow, 并核对 UE 原始
ue_event_reaction 与 quality_shadow 的值是否一致。

不写库、不调 LLM、不下单.
用法:
  PYTHONPATH=. ./venv/bin/python scripts/quality_shadow_event_reaction_audit.py --since 2026-10-22
  ... --date 2026-10-22 --json
退出码: 0=PASS/PASS_NO_REACTION, 2=NOT_READY/NO_ACTIONABLE/BATCH_IN_PROGRESS, 1=FAIL
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "ftseek_us.db"
SCAN_PATH = ROOT / "data" / "shadow" / "llm_universe_scan.jsonl"
KEY_FIELDS = (
    "event_age_h",
    "post_event_move_pct",
    "reaction_basis",
    "reaction_source",
    "reaction_reason",
)


def _obj(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def _ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def _conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def latest_da_date(conn: sqlite3.Connection) -> Optional[str]:
    row = conn.execute(
        "SELECT MAX(date) FROM playbook_instructions WHERE decision_id LIKE 'DA-%'"
    ).fetchone()
    return str(row[0]) if row and row[0] else None


def load_rows(
    db_path: Path = DB_PATH,
    *,
    date: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    conn = _conn(db_path)
    try:
        target = date
        if not target:
            if since:
                row = conn.execute(
                    "SELECT MAX(date) FROM playbook_instructions "
                    "WHERE decision_id LIKE 'DA-%' AND date >= ?", (since,)
                ).fetchone()
                target = str(row[0]) if row and row[0] else None
            else:
                target = latest_da_date(conn)
        if not target:
            return []
        rows = conn.execute(
            """SELECT id,date,symbol,direction,decision_id,
                      metadata_json,raw_instruction_json,created_at,updated_at
               FROM playbook_instructions
               WHERE date = ? AND decision_id LIKE 'DA-%'
               ORDER BY id LIMIT ?""",
            (target, int(limit)),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def load_da_feed_scan(
    target_date: Optional[str],
    scan_path: Path = SCAN_PATH,
) -> Dict[str, Any]:
    """读取目标日 da_feed scan, 供审计区分"输入没反应"与"反应未落库"."""
    empty = {
        "found": False,
        "scanned_at": None,
        "brief_template_version": None,
        "surfaced_n": 0,
        "event_reaction_coverage": 0,
        "reaction_basis": {},
    }
    if not target_date or not Path(scan_path).exists():
        return empty
    latest = None
    bad_lines = 0
    try:
        for line in Path(scan_path).read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except (TypeError, ValueError):
                bad_lines += 1  # 坏 JSONL 行跳过, 汇总留痕
                record = None
            if not isinstance(record, dict):
                continue
            if (record.get("type") == "scan"
                    and record.get("scan_window") == "da_feed"
                    and str(record.get("date")) == str(target_date)):
                latest = record
    except OSError:
        return empty
    if bad_lines:
        print(f"[event-reaction-audit] warn: {bad_lines} 行坏 JSONL 已跳过 ({scan_path})")
    if not latest:
        return empty
    surfaced = latest.get("surfaced") or []
    basis = Counter()
    coverage = 0
    for item in surfaced:
        reaction = item.get("evidence_matched_event_reaction")
        if isinstance(reaction, dict) and reaction.get("post_event_move_pct") is not None:
            coverage += 1
            basis[str(reaction.get("reaction_basis") or "MISSING")] += 1
    return {
        "found": True,
        "scanned_at": latest.get("scanned_at"),
        "brief_template_version": latest.get("brief_template_version"),
        "surfaced_n": len(surfaced),
        "event_reaction_coverage": coverage,
        "reaction_basis": dict(basis),
    }


def apply_feed_gate(result: Dict[str, Any], scan: Dict[str, Any]) -> Dict[str, Any]:
    """输入 da_feed 有反应、但 DA raw/quality_shadow 全没有 ⇒ FAIL (链路丢字段)."""
    if (result.get("status", "").startswith("PASS")
            and result.get("raw_reaction_rows") == 0
            and int((scan or {}).get("event_reaction_coverage") or 0) > 0):
        out = dict(result)
        out["status"] = "FAIL"
        out["reason"] = "feed_reaction_not_persisted"
        return out
    return result


def _same_float(left: Any, right: Any, tol: float) -> bool:
    try:
        return abs(float(left) - float(right)) <= tol
    except (TypeError, ValueError):
        return left is right


def audit_rows(
    rows: List[Dict[str, Any]],
    *,
    now: Optional[datetime] = None,
    maturity_min: float = 5.0,
) -> Dict[str, Any]:
    total = len(rows)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    stamps = [
        stamp for stamp in (
            # created_at = 指令生成/批次写入时刻; updated_at 会被执行/结算/元数据更新刷新,
            # 不能用来判断 DA 批次是否写完 (10/21 实测: 12:55 写入, 13:57 仍有行被结算更新).
            _ts(row.get("created_at")) or _ts(row.get("updated_at"))
            for row in rows
        ) if stamp is not None
    ]
    latest_ts = max(stamps) if stamps else None
    batch_age_min = (
        round((now - latest_ts).total_seconds() / 60.0, 1)
        if latest_ts is not None else None
    )
    batch_mature = batch_age_min is None or batch_age_min >= float(maturity_min)
    actionable = [
        row for row in rows
        if str(row.get("direction") or "").upper() != "SKIP"
    ]
    actionable_n = len(actionable)
    skip_rows = total - actionable_n
    qs_present = 0
    key_counts = Counter()
    non_null_pct = 0
    raw_reaction_rows = 0
    raw_matches = 0
    mismatches: List[Dict[str, Any]] = []
    origins = Counter()
    directions = Counter()

    for row in actionable:
        symbol = row.get("symbol")
        metadata = _obj(row.get("metadata_json"))
        qs = metadata.get("quality_shadow")
        if not isinstance(qs, dict) or not qs:
            continue
        qs_present += 1
        origins[str(qs.get("origin") or "UNKNOWN")] += 1
        directions[str(qs.get("direction") or row.get("direction") or "UNKNOWN")] += 1
        for key in KEY_FIELDS:
            if key in qs:
                key_counts[key] += 1
        if qs.get("post_event_move_pct") is not None:
            non_null_pct += 1

        raw = _obj(row.get("raw_instruction_json"))
        reaction = raw.get("ue_event_reaction")
        if not isinstance(reaction, dict) or not reaction:
            continue
        raw_reaction_rows += 1
        if reaction.get("post_event_move_pct") is None:
            continue
        problems = []
        if not _same_float(qs.get("post_event_move_pct"),
                           reaction.get("post_event_move_pct"), 0.02):
            problems.append("post_event_move_pct")
        if not _same_float(qs.get("event_age_h"),
                           reaction.get("age_h"), 0.2):
            problems.append("age_h")
        if str(qs.get("reaction_basis") or "") != str(
                reaction.get("reaction_basis") or ""):
            problems.append("reaction_basis")
        if problems:
            mismatches.append({
                "id": row.get("id"),
                "symbol": symbol,
                "fields": problems,
                "raw": {k: reaction.get(k) for k in
                        ("age_h", "post_event_move_pct", "reaction_basis")},
                "quality_shadow": {k: qs.get(k) for k in
                                   ("event_age_h", "post_event_move_pct",
                                    "reaction_basis")},
            })
        else:
            raw_matches += 1

    key_full = (
        all(key_counts[key] == actionable_n for key in KEY_FIELDS)
        if actionable_n else False
    )
    if total == 0:
        status = "NOT_READY"
    elif not batch_mature:
        status = "BATCH_IN_PROGRESS"
    elif actionable_n == 0:
        status = "NO_ACTIONABLE"
    elif qs_present != actionable_n or not key_full:
        status = "FAIL"
    elif raw_reaction_rows and (raw_matches == 0 or mismatches):
        status = "FAIL"
    elif raw_reaction_rows:
        status = "PASS"
    else:
        status = "PASS_NO_REACTION"

    return {
        "status": status,
        "total_da_rows": total,
        "actionable_rows": actionable_n,
        "skip_rows": skip_rows,
        "quality_shadow_rows": qs_present,
        "batch_mature": batch_mature,
        "batch_age_min": batch_age_min,
        "maturity_min": float(maturity_min),
        "latest_row_ts": latest_ts.isoformat() if latest_ts else None,
        "key_coverage": {key: key_counts[key] for key in KEY_FIELDS},
        "key_full_coverage": key_full,
        "post_event_move_pct_nonnull": non_null_pct,
        "raw_reaction_rows": raw_reaction_rows,
        "raw_reaction_matches": raw_matches,
        "mismatches": mismatches,
        "origins": dict(origins),
        "directions": dict(directions),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="quality_shadow event_reaction 落库验收 (只读)")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--date", default=None)
    parser.add_argument("--since", default=None)
    parser.add_argument("--scan-path", type=Path, default=SCAN_PATH)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--maturity-min", type=float, default=5.0,
                        help="最新 DA 行需静默多少分钟才判批次已成熟")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    rows = load_rows(args.db, date=args.date, since=args.since, limit=args.limit)
    result = audit_rows(rows, maturity_min=args.maturity_min)
    target_date = str(rows[0].get("date")) if rows else (args.date or args.since)
    scan = load_da_feed_scan(target_date, args.scan_path)
    result = apply_feed_gate(result, scan)
    result["da_feed_scan"] = scan
    result["date"] = target_date or "latest"
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"DATE {result['date']} status={result['status']}")
        print(f"  DA rows={result['total_da_rows']} "
              f"actionable={result['actionable_rows']} "
              f"skip={result['skip_rows']} "
              f"quality_shadow={result['quality_shadow_rows']} "
              f"key_full={result['key_full_coverage']}")
        print(f"  batch_mature={result['batch_mature']} "
              f"age_min={result['batch_age_min']} "
              f"latest={result['latest_row_ts']}")
        print(f"  da_feed_scan={result.get('da_feed_scan')}")
        if result.get("reason"):
            print(f"  reason={result['reason']}")
        print(f"  key_coverage={result['key_coverage']}")
        print(f"  reaction nonnull={result['post_event_move_pct_nonnull']} "
              f"raw_reaction_rows={result['raw_reaction_rows']} "
              f"matches={result['raw_reaction_matches']} "
              f"mismatches={len(result['mismatches'])}")
        print(f"  origins={result['origins']} directions={result['directions']}")
        for item in result["mismatches"][:5]:
            print(f"  MISMATCH {item}")

    if result["status"] in ("NOT_READY", "NO_ACTIONABLE", "BATCH_IN_PROGRESS"):
        return 2
    return 0 if result["status"].startswith("PASS") else 1


if __name__ == "__main__":
    raise SystemExit(main())
