#!/usr/bin/env python3
"""Offline bucket-lineage audit and metadata enrichment.

Default mode is dry-run against a read-only SQLite connection.  ``--write`` is
required to merge ``bucket_lineage`` into
``trade_records.metadata_json``; all other metadata keys are preserved.

The script does not create an exit, change a stop/trail, open a shadow, or write
to ``playbook_instructions``.  It is safe to run repeatedly: rows whose
deterministic ``source_hash`` is unchanged are skipped.

Usage:
  PYTHONPATH=. ./venv/bin/python scripts/enrich_bucket_lineage.py \
      --since 2026-06-26 --until 2026-10-26
  PYTHONPATH=. ./venv/bin/python scripts/enrich_bucket_lineage.py \
      --since 2026-06-26 --until 2026-10-26 --write
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "ftseek_us.db"
DEFAULT_CONFIG = ROOT / "config" / "playbook_horizon.yaml"
DEFAULT_AUDIT_DIR = ROOT / "data" / "audits"

from app.playbook.bucket_lineage import (  # noqa: E402
    build_bucket_lineage,
    lineage_needs_update,
    parse_json_object,
    select_vol_bucket,
)
from app.playbook.invalidation_contract import extract_text_price  # noqa: E402


def _connect(db_path: Path, readonly: bool) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _load_yaml(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    return data if isinstance(data, dict) else {}


def _load_trades(
    conn: sqlite3.Connection,
    since: Optional[str],
    until: Optional[str],
    symbol: Optional[str],
    limit: Optional[int],
) -> tuple[List[sqlite3.Row], int]:
    sql = (
        "SELECT id, symbol, direction, entry_price, entry_time, trade_date, metadata_json "
        "FROM trade_records"
    )
    params: list[Any] = []
    if since:
        sql += " WHERE substr(entry_time, 1, 10) >= ?"
        params.append(since)
    if until:
        sql += (" AND " if since else " WHERE ") + "substr(entry_time, 1, 10) <= ?"
        params.append(until)
    if symbol:
        sql += (" AND " if since or until else " WHERE ") + "symbol = ?"
        params.append(symbol)
    sql += " ORDER BY id"
    rows = list(conn.execute(sql, params))

    candidates = 0
    if limit:
        rows = rows[:limit]
    for row in rows:
        meta = parse_json_object(row["metadata_json"])
        if meta.get("playbook_instruction_id") not in (None, ""):
            candidates += 1
    return rows, candidates


def _instruction_id(metadata: Mapping[str, Any]) -> Optional[int]:
    value = metadata.get("playbook_instruction_id")
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number > 0 else None


def _load_instructions(
    conn: sqlite3.Connection, ids: Iterable[int]
) -> Dict[int, Dict[str, Any]]:
    values = sorted(set(ids))
    if not values:
        return {}
    placeholders = ",".join("?" for _ in values)
    sql = (
        "SELECT id, date, symbol, direction, entry_price, stop_loss, horizon, "
        "vol_bucket, atr_pct, invalidation, raw_instruction_json, metadata_json "
        f"FROM playbook_instructions WHERE id IN ({placeholders})"
    )
    return {int(row["id"]): dict(row) for row in conn.execute(sql, values)}


def _horizon_bucket_config(
    horizon_config: Mapping[str, Any], horizon: Any
) -> tuple[str, Mapping[str, Any], Optional[str]]:
    horizon_name = str(horizon or "short").strip().lower()
    params = horizon_config.get(f"{horizon_name}_horizon")
    if not isinstance(params, dict):
        horizon_name = "short"
        params = horizon_config.get("short_horizon") or {}
    buckets = params.get("vol_buckets") if isinstance(params, dict) else {}
    if not isinstance(buckets, dict):
        buckets = {}
    version = params.get("bucket_schema_version") if isinstance(params, dict) else None
    return horizon_name, buckets, str(version) if version else None


def _instruction_bucket_audit(
    conn: sqlite3.Connection,
    horizon_config: Mapping[str, Any],
    since: Optional[str],
    until: Optional[str],
    symbol: Optional[str],
) -> Dict[str, Any]:
    sql = (
        "SELECT id, date, symbol, direction, horizon, atr_pct, vol_bucket, metadata_json "
        "FROM playbook_instructions"
    )
    params: list[Any] = []
    if since:
        sql += " WHERE date >= ?"
        params.append(since)
    if until:
        sql += (" AND " if since else " WHERE ") + "date <= ?"
        params.append(until)
    if symbol:
        sql += (" AND " if since or until else " WHERE ") + "symbol = ?"
        params.append(symbol)
    sql += " ORDER BY id"

    stats: Dict[str, Any] = {
        "rows": 0,
        "actionable_rows": 0,
        "actionable_with_atr": 0,
        "actionable_missing_atr": 0,
        "actionable_missing_bucket": 0,
        "actionable_bucket_mismatch": 0,
        "actionable_deprecated_bucket_hits": 0,
        "with_atr": 0,
        "with_bucket": 0,
        "missing_atr": 0,
        "missing_bucket": 0,
        "bucket_mismatch": 0,
        "deprecated_bucket_hits": 0,
        "reprice_rows": 0,
        "reprice_bucket_switches": 0,
        "schema_version_counts": Counter(),
        "effective_reprice_bucket_counts": Counter(),
    }
    examples = []
    for row in conn.execute(sql, params):
        stats["rows"] += 1
        _name, buckets, version = _horizon_bucket_config(horizon_config, row["horizon"])
        stats["schema_version_counts"][version or "missing"] += 1
        atr = row["atr_pct"]
        bucket = str(row["vol_bucket"] or "").strip() or None
        expected = select_vol_bucket(atr, buckets)
        actionable = str(row["direction"] or "").upper() in ("BUY", "SELL_SHORT")
        if actionable:
            stats["actionable_rows"] += 1
            if atr in (None, "", 0, 0.0):
                stats["actionable_missing_atr"] += 1
            else:
                stats["actionable_with_atr"] += 1
            if not bucket:
                stats["actionable_missing_bucket"] += 1
        if atr in (None, "", 0, 0.0):
            stats["missing_atr"] += 1
        else:
            stats["with_atr"] += 1
        if bucket:
            stats["with_bucket"] += 1
        else:
            stats["missing_bucket"] += 1
        if bucket and expected and bucket != expected:
            stats["bucket_mismatch"] += 1
            if actionable:
                stats["actionable_bucket_mismatch"] += 1
            if len(examples) < 10:
                examples.append({
                    "instruction_id": row["id"], "symbol": row["symbol"],
                    "stored": bucket, "expected": expected, "atr_pct": atr,
                })
        if bucket and isinstance(buckets.get(bucket), dict) and buckets[bucket].get(
            "deprecated_unreachable"
        ):
            stats["deprecated_bucket_hits"] += 1
            if actionable:
                stats["actionable_deprecated_bucket_hits"] += 1

        meta = parse_json_object(row["metadata_json"])
        reprice = meta.get("reprice_audit")
        if isinstance(reprice, dict):
            stats["reprice_rows"] += 1
            repriced = str(reprice.get("vol_bucket") or "").strip() or None
            if repriced:
                stats["effective_reprice_bucket_counts"][repriced] += 1
            if repriced and bucket and repriced != bucket:
                stats["reprice_bucket_switches"] += 1
    stats["bucket_mismatch_examples"] = examples
    stats["schema_version_counts"] = dict(stats["schema_version_counts"])
    stats["effective_reprice_bucket_counts"] = dict(
        stats["effective_reprice_bucket_counts"]
    )
    return stats


def _audit(
    *,
    db_path: Path,
    config_path: Path,
    since: Optional[str],
    until: Optional[str],
    symbol: Optional[str],
    limit: Optional[int],
    write: bool,
    audit_dir: Path,
) -> Dict[str, Any]:
    horizon_config = _load_yaml(config_path)
    conn = _connect(db_path, readonly=not write)
    try:
        rows, candidates = _load_trades(conn, since, until, symbol, limit)
        instruction_ids = []
        trade_meta_by_id: Dict[int, Dict[str, Any]] = {}
        for row in rows:
            meta = parse_json_object(row["metadata_json"])
            trade_meta_by_id[int(row["id"])] = meta
            pid = _instruction_id(meta)
            if pid is not None:
                instruction_ids.append(pid)
        instructions = _load_instructions(conn, instruction_ids)

        available_instruction_ids = set(instructions)
        joined = sum(
            1
            for row in rows
            if _instruction_id(trade_meta_by_id[int(row["id"])])
            in available_instruction_ids
        )

        results = []
        updates = []
        effective_counts = Counter()
        generated_alignment = Counter()
        repriced_alignment = Counter()
        missing_counts = Counter()
        cap_breaches = []
        bucket_switches = 0
        repriced_count = 0
        incomplete = 0
        errors = 0
        unchanged = 0

        for row in rows:
            trade_id = int(row["id"])
            meta = trade_meta_by_id[trade_id]
            pid = _instruction_id(meta)
            if pid is None:
                results.append({
                    "trade_id": trade_id, "symbol": row["symbol"],
                    "status": "unjoined", "reason": "missing_instruction_id",
                })
                continue
            instruction = instructions.get(pid)
            if instruction is None:
                results.append({
                    "trade_id": trade_id, "symbol": row["symbol"],
                    "instruction_id": pid, "status": "unjoined",
                    "reason": "instruction_not_found",
                })
                continue
            horizon = meta.get("horizon") or instruction.get("horizon") or "short"
            _h, buckets, schema_version = _horizon_bucket_config(
                horizon_config, horizon
            )
            try:
                lineage = build_bucket_lineage(
                    instruction=instruction,
                    trade=dict(row),
                    vol_buckets=buckets,
                    bucket_schema_version=schema_version,
                    invalidation_text_price=extract_text_price(
                        instruction.get("invalidation"),
                        entry_price=instruction.get("entry_price"),
                    ),
                )
            except Exception as exc:
                errors += 1
                results.append({
                    "trade_id": trade_id,
                    "symbol": row["symbol"],
                    "instruction_id": pid,
                    "status": "error",
                    "reason": f"{type(exc).__name__}: {exc}",
                })
                continue
            effective_counts[str(lineage.get("bucket_effective") or "null")] += 1
            generated_alignment[str(lineage.get("bucket_generated_alignment"))] += 1
            repriced_alignment[str(lineage.get("bucket_repriced_alignment"))] += 1
            for field in lineage.get("missing_fields") or []:
                missing_counts[field] += 1
            if lineage.get("bucket_repriced"):
                repriced_count += 1
                if lineage.get("bucket_repriced") != lineage.get("bucket_generated"):
                    bucket_switches += 1
            if not lineage.get("input_complete"):
                incomplete += 1
            breach = lineage.get("sl_cap_breach_bps")
            if breach is not None and breach > 0:
                cap_breaches.append({
                    "trade_id": trade_id,
                    "symbol": row["symbol"],
                    "bucket_effective": lineage.get("bucket_effective"),
                    "sl_pct_from_fill": lineage.get("sl_pct_from_fill"),
                    "cap_pct": lineage.get("bucket_sl_cap_pct"),
                    "breach_bps": breach,
                })

            existing = meta.get("bucket_lineage")
            changed = lineage_needs_update(existing, lineage)
            status = "unchanged"
            if changed:
                status = "would_write" if not write else "pending_write"
                if write:
                    merged = dict(meta)
                    merged["bucket_lineage"] = lineage
                    updates.append((trade_id, merged))
            else:
                unchanged += 1
            results.append({
                "trade_id": trade_id,
                "symbol": row["symbol"],
                "instruction_id": pid,
                "status": status,
                "lineage": lineage,
            })

        written = 0
        if write and updates:
            with conn:
                for trade_id, merged in updates:
                    conn.execute(
                        "UPDATE trade_records SET metadata_json=? WHERE id=?",
                        (json.dumps(merged, ensure_ascii=False, default=str), trade_id),
                    )
                    written += 1
            for item in results:
                if item.get("status") == "pending_write":
                    item["status"] = "written"

        instruction_audit = _instruction_bucket_audit(
            conn, horizon_config, since, until, symbol
        )
    finally:
        conn.close()

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    audit_dir.mkdir(parents=True, exist_ok=True)
    audit_path = audit_dir / f"bucket_lineage_{timestamp}.jsonl"
    with open(audit_path, "w", encoding="utf-8") as handle:
        for item in results:
            handle.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")

    summary = {
        "window": {"since": since, "until": until, "symbol": symbol},
        "trade_rows_scanned": len(rows),
        "join_candidates": candidates,
        "joined": joined,
        "unjoined": len(rows) - joined,
        "computed": len([item for item in results if item.get("lineage")]),
        "written": written,
        "unchanged": unchanged,
        "errors": errors,
        "incomplete_lineage": incomplete,
        "effective_bucket_counts": dict(effective_counts),
        "generated_bucket_alignment": dict(generated_alignment),
        "repriced_bucket_alignment": dict(repriced_alignment),
        "repriced_rows": repriced_count,
        "reprice_bucket_switches": bucket_switches,
        "sl_cap_breaches": len(cap_breaches),
        "sl_cap_breach_examples": cap_breaches[:20],
        "missing_field_counts": dict(missing_counts),
        "instruction_bucket_audit": instruction_audit,
        "audit_path": str(audit_path),
        "write_enabled": bool(write),
    }
    return summary


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Bucket lineage audit/enrichment (offline)")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--since", default=None, help="entry ET date >= YYYY-MM-DD")
    parser.add_argument("--until", default=None, help="entry ET date <= YYYY-MM-DD")
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--audit-dir", default=str(DEFAULT_AUDIT_DIR))
    parser.add_argument("--write", action="store_true", help="merge bucket_lineage metadata")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"[bucket-lineage] DB not found: {db_path}")
        return 2
    summary = _audit(
        db_path=db_path,
        config_path=Path(args.config),
        since=args.since,
        until=args.until,
        symbol=args.symbol,
        limit=args.limit,
        write=args.write,
        audit_dir=Path(args.audit_dir),
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    print(f"[bucket-lineage] audit -> {summary['audit_path']}")
    return 0 if summary["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
