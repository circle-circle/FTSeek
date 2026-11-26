#!/usr/bin/env python3
"""Read-only DA source-clock inventory; coverage is not real-time freshness."""
from __future__ import annotations

import argparse
import json
import math
import re
import statistics
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
CKPT_DIR = ROOT / "data" / "da_checkpoint"
SKIP_DIMENSIONS = {"symbol", "sector_context", "ue_candidate_context"}
INFORMATION_ROLES = {"published", "observed", "bar_date"}
CLOCK_ROLES = {
    "publish_time": "published", "published_at": "published",
    "update_time": "observed", "snapshot_time": "observed",
    "price_as_of": "observed", "current_asof": "bar_date",
    "as_of": "observed", "asof": "observed",
    "capital_flow_item_time": "observed",
    "timestamp": "collected", "_timestamp": "collected",
    "fetched_at": "collected", "collected_at": "collected", "saved_at": "collected",
    "last_valid_time": "provider_clock", "date": "event_date",
}


def _is_asof_key(key: str) -> bool:
    return str(key).lower() in CLOCK_ROLES or str(key).lower() == "time"


def _parse_ts(value: Any) -> Optional[datetime]:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if re.fullmatch(r"(?:\d{10}|\d{13})(?:\.\d+)?", text):
        epoch = float(text)
        if not math.isfinite(epoch):
            return None
        if len(text.split(".")[0]) == 13:
            epoch /= 1000
        try:
            return datetime.fromtimestamp(epoch, tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?)?", text):
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("America/New_York"))
    return parsed


def _records(dimension: str, value: Any) -> list[tuple[str, Any]]:
    if isinstance(value, list):
        return [(f"[{i}]", item) for i, item in enumerate(value)]
    if not isinstance(value, dict):
        return [("", value)]
    if dimension == "market_context" and "_error" not in value:
        records = []
        for name in ("vix", "spy", "qqq"):
            item = value.get(name)
            records.append((name, item))
            if isinstance(item, dict) and "snapshot" in item:
                records.append((f"{name}.snapshot", item["snapshot"]))
        return records
    list_key = {
        "industry_peers": "peers", "capital_flow": "data",
        "insider_transactions": "recent_transactions",
        "earnings_calendar": "all_upcoming",
    }.get(dimension)
    if list_key and isinstance(value.get(list_key), list) and value[list_key]:
        return [(f"{list_key}[{i}]", item) for i, item in enumerate(value[list_key])]
    if dimension == "earnings_calendar" and isinstance(value.get("next_earnings"), dict):
        return [("next_earnings", value["next_earnings"])]
    return [("", value)]


def _clocks(dimension: str, path: str, record: Any) -> list[tuple[str, str, Any]]:
    if not isinstance(record, dict):
        return []
    clocks = []
    for key, raw in record.items():
        if not _is_asof_key(key):
            continue
        normalized = key.lower()
        role = CLOCK_ROLES.get(normalized)
        if normalized == "time":
            if dimension in {"news", "news_v2", "research", "sentiment"}:
                if "publish_time" in record or "published_at" in record:
                    continue
                role = "published"
            else:
                role = "bar_date" if dimension == "kline" else "event_date"
        if normalized == "date" and dimension == "kline":
            role = "bar_date"
        clocks.append((f"{path}.{key}" if path else key, role, raw))
    return clocks


def _asof_candidates(value: Any, dimension: str = "") -> list[tuple[str, Any]]:
    return [(key, raw) for path, record in _records(dimension, value)
            for key, role, raw in _clocks(dimension, path, record) if role in INFORMATION_ROLES]


def _best_asof(value: Any, dimension: str = "") -> tuple[Optional[str], Optional[Any]]:
    parsed = [(ts, key, raw) for key, raw in _asof_candidates(value, dimension)
              if (ts := _parse_ts(raw)) is not None]
    if not parsed:
        return None, None
    _, key, raw = min(parsed, key=lambda item: item[0])
    return key, raw


def _dimension_flags(dimension: str, value: Any, source_status: Dict[str, Any]) -> Dict[str, bool]:
    degraded = isinstance(value, dict) and ("_error" in value or str(value.get("status") or "").lower() in {"error", "failed"})
    fallback = False
    for _, record in _records(dimension, value):
        if isinstance(record, dict):
            basis = str(record.get("as_of_basis") or record.get("caliber") or "").lower()
            fallback |= "prev_close" in basis or "上一交易日" in basis
    status = source_status.get(dimension)
    if status is not None and str(status).lower() != "ok":
        degraded = True
    return {"degraded": degraded, "fallback": fallback}


def _finish_ages(bucket: dict) -> None:
    ages = bucket.pop("ages_seconds")
    bucket["age_sample_n"] = len(ages)
    for name, function in (("median", statistics.median), ("min", min), ("max", max)):
        bucket[f"age_seconds_{name}"] = round(function(ages), 1) if ages else None


def build_manifest(day: str, root: Optional[Path] = None) -> Dict[str, Any]:
    root = Path(root or CKPT_DIR) / day
    files = sorted(root.glob("*.raw.json")) if root.exists() else []
    dimensions = {}
    quality_counts, vix_basis, snapshot_sessions, source_status_keys = (Counter() for _ in range(4))
    parse_errors, missing_reference_n = 0, 0
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or not isinstance(payload.get("collected"), dict):
                raise ValueError("invalid raw shape")
            if any(not isinstance(payload["collected"].get(key) or {}, dict) for key in ("_source_status", "_quality")):
                raise ValueError("invalid source metadata shape")
        except (OSError, ValueError):
            parse_errors += 1
            continue
        collected = payload["collected"]
        saved_at = _parse_ts(payload.get("saved_at"))
        # Archive time is rewritten after synthesis; collection time is not a substitute.
        missing_reference_n += int(saved_at is None)
        source_status = collected.get("_source_status") or {}
        quality = collected.get("_quality") or {}
        quality_counts[f"{quality.get('ok')}/{quality.get('total')}"] += 1
        market = collected.get("market_context") or {}
        vix = market.get("vix") or {} if isinstance(market, dict) else {}
        vix_basis[str(vix.get("as_of_basis") or vix.get("caliber") or "missing") if isinstance(vix, dict) else "missing"] += 1
        snapshot = collected.get("snapshot") or {}
        snapshot_sessions[str(snapshot.get("session") or "missing") if isinstance(snapshot, dict) else "missing"] += 1
        source_status_keys.update(source_status.keys())
        for dimension, value in collected.items():
            if dimension.startswith("_") or dimension in SKIP_DIMENSIONS:
                continue
            bucket = dimensions.setdefault(dimension, {
                "total_n": 0, "asof_n": 0, "missing_n": 0, "degraded_n": 0, "fallback_n": 0,
                "record_n": 0, "timed_record_n": 0, "unknown_record_n": 0,
                "invalid_clock_n": 0, "missing_clock_n": 0, "future_information_clock_n": 0,
                "ages_seconds": [], "missing_symbols": [], "clock_roles": {},
                "sample_asof_key": None, "sample_asof_value": None,
            })
            bucket["total_n"] += 1
            for flag, enabled in _dimension_flags(dimension, value, source_status).items():
                bucket[f"{flag}_n"] += int(enabled)
            timed_records = 0
            sample_key, sample_value = _best_asof(value, dimension)
            for record_path, record in _records(dimension, value):
                bucket["record_n"] += 1
                information_times = []
                for key, role, raw in _clocks(dimension, record_path, record):
                    stats = bucket["clock_roles"].setdefault(role, {
                        "clock_n": 0, "parsed_n": 0, "invalid_n": 0, "missing_n": 0, "future_n": 0,
                        "date_only_n": 0, "ages_seconds": [], "sample_path": key,
                    })
                    stats["clock_n"] += 1
                    if raw is None or (isinstance(raw, str) and not raw.strip()):
                        stats["missing_n"] += 1
                        bucket["missing_clock_n"] += 1
                        continue
                    ts = _parse_ts(raw)
                    if ts is None:
                        stats["invalid_n"] += 1
                        bucket["invalid_clock_n"] += 1
                        continue
                    stats["parsed_n"] += 1
                    stats["date_only_n"] += int(bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(raw))))
                    future = saved_at is not None and ts > saved_at
                    stats["future_n"] += int(future)
                    if role in INFORMATION_ROLES and not future:
                        information_times.append(ts)
                    if future and role in INFORMATION_ROLES:
                        bucket["future_information_clock_n"] += 1
                    if saved_at is not None and (role not in INFORMATION_ROLES or not future):
                        stats["ages_seconds"].append((saved_at - ts).total_seconds())
                timed_records += bool(information_times)
                if information_times and saved_at is not None:
                    bucket["ages_seconds"].append((saved_at - min(information_times)).total_seconds())
            bucket["timed_record_n"] += timed_records
            if timed_records:
                bucket["asof_n"] += 1
                if bucket["sample_asof_key"] is None:
                    bucket["sample_asof_key"], bucket["sample_asof_value"] = sample_key, sample_value
            else:
                bucket["missing_n"] += 1
                if len(bucket["missing_symbols"]) < 12:
                    bucket["missing_symbols"].append(payload.get("symbol") or path.stem)
    dimension_rows = []
    for dimension, bucket in sorted(dimensions.items()):
        bucket["unknown_record_n"] = bucket["record_n"] - bucket["timed_record_n"]
        _finish_ages(bucket)
        for stats in bucket["clock_roles"].values():
            _finish_ages(stats)
        dimension_rows.append(dict(bucket, dimension=dimension))
    return {
        "day": day, "generated_at": datetime.now(timezone.utc).isoformat(),
        "schema": "da_freshness_clock_inventory_v2", "raw_count": len(files),
        "parse_error_n": parse_errors, "missing_archive_reference_n": missing_reference_n,
        "quality_counts": dict(quality_counts), "vix_as_of_basis": dict(vix_basis),
        "snapshot_sessions": dict(snapshot_sessions), "source_status_keys": dict(source_status_keys),
        "dimensions": dimension_rows, "decision_use": "display_only_not_a_trading_gate",
        "age_basis": "raw.saved_at archive time, not model decision time; naive clocks use ET",
        "age_population": "one oldest valid non-future information clock per source record; not newest per source",
        "bar_date_semantics": "daily-bar dates use ET midnight, not the actual closing instant or quote latency",
        "clock_scope": "known raw source paths only; collection/provider/event clocks do not establish information freshness",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="DA raw 输入新鲜度清单 (只读)")
    parser.add_argument("--date", required=True)
    parser.add_argument("--root", type=Path, default=CKPT_DIR)
    args = parser.parse_args()
    print(json.dumps(build_manifest(args.date, args.root), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
