#!/usr/bin/env python3
"""DA raw 输入 per-source as-of / session / age / fallback 只读清单。

用法:
  PYTHONPATH=. ./venv/bin/python scripts/da_input_freshness_manifest.py --date 2026-11-10

边界: 只读披露，不写 prompt、不接交易门、不改 DB。覆盖≠实时。
"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
CKPT_DIR = ROOT / "data" / "da_checkpoint"
SKIP_DIMENSIONS = {"symbol", "sector_context", "ue_candidate_context"}


def _is_asof_key(key: str) -> bool:
    normalized = str(key).lower()
    if normalized == "time_range":
        return False
    return normalized in {"time", "date"} or any(
        token in normalized
        for token in ("as_of", "publish_time", "update_time", "timestamp", "snapshot_time")
    )


def _parse_ts(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    # DA raw 的 naive 时间戳是 ET wall time，不是 UTC；epoch 串支持秒/毫秒。
    if text.isdigit() and len(text) in (10, 13):
        epoch = int(text) / (1000.0 if len(text) == 13 else 1.0)
        return datetime.fromtimestamp(epoch, tz=timezone.utc)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(text[:19], fmt)
            except ValueError:
                parsed = None
            if parsed is not None:
                break
        else:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("America/New_York"))
    return parsed


def _asof_candidates(value: Any) -> list[tuple[str, Any]]:
    candidates: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        candidates.extend((key, value[key]) for key in value if _is_asof_key(key))
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                candidates.extend((key, item[key]) for key in item if _is_asof_key(key))
    return candidates


def _best_asof(value: Any) -> tuple[Optional[str], Optional[Any]]:
    candidates = _asof_candidates(value)
    if not candidates:
        return None, None
    parsed = [(_parse_ts(raw), key, raw) for key, raw in candidates]
    parsed = [item for item in parsed if item[0] is not None]
    if parsed:
        _, key, raw = max(parsed, key=lambda item: item[0])
        return key, raw
    return candidates[0]


def _dimension_flags(dimension: str, value: Any, source_status: Dict[str, Any]) -> Dict[str, bool]:
    degraded = False
    fallback = False
    if isinstance(value, dict):
        degraded = "_error" in value or str(value.get("status") or "").lower() in {"error", "failed"}
        basis = str(value.get("as_of_basis") or value.get("caliber") or "").lower()
        fallback = "prev_close" in basis or "上一交易日" in basis
    status = source_status.get(dimension)
    if status is not None and str(status).lower() != "ok":
        degraded = True
    if dimension == "market_context" and isinstance(value, dict):
        vix = value.get("vix") or {}
        if str(vix.get("as_of_basis") or "").lower() == "prev_close":
            fallback = True
    return {"degraded": degraded, "fallback": fallback}


def build_manifest(day: str, root: Optional[Path] = None) -> Dict[str, Any]:
    root = Path(root or CKPT_DIR) / day
    files = sorted(root.glob("*.raw.json")) if root.exists() else []
    dimensions: Dict[str, Dict[str, Any]] = {}
    quality_counts: Counter = Counter()
    vix_basis: Counter = Counter()
    snapshot_sessions: Counter = Counter()
    source_status_keys: Counter = Counter()
    parse_errors = 0
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        except (OSError, ValueError):
            parse_errors += 1
            continue
        collected = payload.get("collected") or {}
        saved_at = _parse_ts(payload.get("saved_at") or collected.get("_timestamp"))
        source_status = collected.get("_source_status") or {}
        quality = collected.get("_quality") or {}
        quality_counts[f"{quality.get('ok')}/{quality.get('total')}"] += 1
        vix = ((collected.get("market_context") or {}).get("vix") or {})
        vix_basis[str(vix.get("as_of_basis") or vix.get("caliber") or "missing")] += 1
        snapshot = collected.get("snapshot") or {}
        snapshot_sessions[str(snapshot.get("session") or "missing")] += 1
        for key in source_status:
            source_status_keys[str(key)] += 1
        for dimension, value in collected.items():
            if dimension.startswith("_") or dimension in SKIP_DIMENSIONS:
                continue
            bucket = dimensions.setdefault(dimension, {
                "total_n": 0, "asof_n": 0, "missing_n": 0, "degraded_n": 0,
                "fallback_n": 0, "ages_seconds": [], "missing_symbols": [],
                "sample_asof_key": None, "sample_asof_value": None,
            })
            bucket["total_n"] += 1
            asof_key, asof_value = _best_asof(value)
            flags = _dimension_flags(dimension, value, source_status)
            bucket["degraded_n"] += int(flags["degraded"])
            bucket["fallback_n"] += int(flags["fallback"])
            if asof_key is None:
                bucket["missing_n"] += 1
                if len(bucket["missing_symbols"]) < 12:
                    bucket["missing_symbols"].append(payload.get("symbol") or path.stem)
                continue
            bucket["asof_n"] += 1
            if bucket["sample_asof_key"] is None:
                bucket["sample_asof_key"] = asof_key
                bucket["sample_asof_value"] = asof_value
            asof_ts = _parse_ts(asof_value)
            if saved_at is not None and asof_ts is not None:
                bucket["ages_seconds"].append(round((saved_at - asof_ts).total_seconds(), 1))
    dimension_rows = []
    for dimension, bucket in sorted(dimensions.items()):
        ages = bucket.pop("ages_seconds")
        bucket["age_seconds_median"] = round(statistics.median(ages), 1) if ages else None
        bucket["age_seconds_max"] = round(max(ages), 1) if ages else None
        bucket["dimension"] = dimension
        dimension_rows.append(bucket)
    return {
        "day": day,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "raw_count": len(files),
        "parse_error_n": parse_errors,
        "quality_counts": dict(quality_counts),
        "vix_as_of_basis": dict(vix_basis),
        "snapshot_sessions": dict(snapshot_sessions),
        "source_status_keys": dict(source_status_keys),
        "dimensions": dimension_rows,
        "decision_use": "display_only_not_a_trading_gate",
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
