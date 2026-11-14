#!/usr/bin/env python3
"""Freeze an offline Event Intelligence case (as-of snapshot + auditable manifest).

Purpose (DEB-20261106 §14-18, §65.4 P0): turn one market day into a reproducible,
offline-only case that only uses events with ``first_seen_at <= as_of``. The
artifact is the shared input for the later paid R1 brief / R2 paired replay; this
script itself makes **zero LLM calls** and **zero production writes**.

What it writes (all under ``--output-dir``, fail-closed: the directory must not exist):
  snapshots/prior_<as_of>.json   optional prior-day negative control snapshot
  snapshots/<as_of>.json         the frozen as-of snapshot
  brief_context.json             deterministic brief context (no LLM), key events pinned
  symbol_coverage.json           pre-LLM symbol/sector coverage matrix
  case_manifest.json             checkpoint paths+SHA256, visibility / no-future checks
  SHA256SUMS.txt                 checksums of every artifact above

Example (NVDA 2026-11-11/12 case):
  venv/bin/python scripts/da_event_case_freeze.py \
    --db data/event_stream.db \
    --as-of 2026-11-12T13:15:00+00:00 \
    --prior-as-of 2026-11-11T13:15:00+00:00 \
    --lookback-hours 24 \
    --raw-root data/da_checkpoint/2026-11-12 \
    --symbols NVDA,AMD,AVGO,TSM,MU,SNDK,ARM,NBIS \
    --key-event-ids 853ef3ee05478043a7c9 \
    --output-dir data/shadow/event_intelligence/nvda_20261112_v1

The script is an offline consumer of app/event_stream/{intelligence,market_brief};
it is not imported by production code (see tests/test_offline_prototype_isolation.py).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.event_stream.intelligence import (  # noqa: E402
    build_event_snapshot,
    iso_utc,
    parse_timestamp,
    read_event_records,
    read_source_health,
)
from app.event_stream.market_brief import (  # noqa: E402
    build_brief_context,
    validate_event_snapshot,
)
from app.event_stream.source_registry import default_source_registry  # noqa: E402
from scripts.da_event_supplemental import (  # noqa: E402
    CASE_SYMBOLS,
    extract_supplemental_evidence,
    merge_supplemental_into_context,
    validate_supplemental_evidence,
)

UTC = timezone.utc
ET = ZoneInfo("America/New_York")

# Case symbol -> text rules. Short tickers are case-sensitive word-boundary matches
# so that e.g. "alarm" never counts as ARM; company names use case-insensitive match.
_SYMBOL_RULES: Dict[str, Sequence[tuple[str, int]]] = {
    "NVDA": (("nvidia", re.IGNORECASE), ("NVDA", 0), ("英伟达", 0)),
    "AMD": (("Advanced Micro Devices", re.IGNORECASE), ("AMD", 0), ("超威半导体", 0)),
    "AVGO": (("Broadcom", re.IGNORECASE), ("AVGO", 0), ("博通", 0)),
    "TSM": (
        ("Taiwan Semiconductor", re.IGNORECASE), ("TSMC", re.IGNORECASE),
        ("TSM", 0), ("台积电", 0),
    ),
    "MU": (("Micron", re.IGNORECASE), ("MU", 0), ("美光", 0)),
    "SNDK": (("SanDisk", re.IGNORECASE), ("SNDK", 0), ("闪迪", 0)),
    "ARM": (("Arm Holdings", re.IGNORECASE), ("ARM", 0), ("安谋", 0)),
    "NBIS": (("Nebius", re.IGNORECASE), ("NBIS", 0)),
}

_SECTOR_RULES = (
    ("nvidia", re.IGNORECASE),
    ("NVDA", 0),
    ("chip", re.IGNORECASE),
    ("chips", re.IGNORECASE),
    ("semiconductor", re.IGNORECASE),
    ("AI", 0),
    ("artificial intelligence", re.IGNORECASE),
    ("export control", re.IGNORECASE),
    ("data center", re.IGNORECASE),
    ("data centre", re.IGNORECASE),
    ("Huang", 0),
)


def _compile(rule: str, flags: int) -> re.Pattern[str]:
    # Boundary on both sides so "HYDROFARM HOLDINGS" never counts as "ARM HOLDINGS".
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(rule)}(?![A-Za-z0-9])", flags)


def _pattern_set(rules: Iterable[tuple[str, int]]) -> List[re.Pattern[str]]:
    return [_compile(rule, flags) for rule, flags in rules]


_SYMBOL_PATTERNS = {symbol: _pattern_set(rules) for symbol, rules in _SYMBOL_RULES.items()}
_SECTOR_PATTERNS = _pattern_set(_SECTOR_RULES)


def symbol_mentioned(text: str, symbol: str) -> bool:
    """Return True when a headline names one of the case symbols."""
    bare = symbol.upper().removeprefix("US.")
    patterns = _SYMBOL_PATTERNS.get(bare)
    if not patterns:
        raise KeyError(f"unknown case symbol: {symbol}")
    return any(pattern.search(text or "") for pattern in patterns)


def sector_mentioned(text: str) -> bool:
    return any(pattern.search(text or "") for pattern in _SECTOR_PATTERNS)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_new(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def _parse_publish(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return datetime.fromtimestamp(float(text), UTC)
    try:
        return parse_timestamp(text)
    except ValueError:
        return None


def _news_publish_times(checkpoint: Dict[str, Any]) -> List[tuple[datetime, str]]:
    collected = checkpoint.get("collected") or {}
    rows: List[tuple[datetime, str]] = []
    for key in ("news", "news_v2"):
        for item in collected.get(key) or []:
            if not isinstance(item, dict):
                continue
            published = _parse_publish(item.get("publish_time"))
            if published is None:
                continue
            title = str(item.get("title") or item.get("headline") or "")
            rows.append((published.astimezone(UTC), title))
    return sorted(rows)


def _nvidia_pattern() -> re.Pattern[str]:
    return re.compile(r"nvidia|NVDA|Huang|英伟达", re.IGNORECASE)


def _relative(path: Path, repo_root: Path = ROOT) -> str:
    try:
        return str(path.resolve().relative_to(repo_root))
    except ValueError:
        return str(path)


def checkpoint_entry(
    raw_path: Path,
    *,
    symbol: str,
    cutoff: datetime,
    repo_root: Path = ROOT,
) -> Dict[str, Any]:
    checkpoint = json.loads(raw_path.read_text(encoding="utf-8"))
    collected = checkpoint.get("collected") or {}
    collected_ts = collected.get("_timestamp")
    collected_at = None
    if isinstance(collected_ts, (int, float)):
        collected_at = datetime.fromtimestamp(float(collected_ts), UTC).isoformat()
    elif collected_ts:
        collected_at = iso_utc(parse_timestamp(str(collected_ts)))
    published = _news_publish_times(checkpoint)
    after_cutoff = [
        {"publish_time": when.isoformat(), "title": title[:180]}
        for when, title in published
        if when > cutoff
    ]
    nvidia_hits = [
        title[:180] for _, title in published
        if _nvidia_pattern().search(title)
    ]
    return {
        "symbol": symbol,
        "path": _relative(raw_path, repo_root),
        "sha256": _sha256_file(raw_path),
        "bytes": raw_path.stat().st_size,
        "trade_date": checkpoint.get("trade_date"),
        "saved_at": checkpoint.get("saved_at"),
        "collected_at": collected_at,
        "user_prompt_sha256": hashlib.sha256(
            str(checkpoint.get("user_prompt") or "").encode("utf-8")
        ).hexdigest() if checkpoint.get("user_prompt") else None,
        "user_prompt_chars": len(str(checkpoint.get("user_prompt") or "")),
        "collected_news_item_count": len(published),
        "latest_news_publish_at": published[-1][0].isoformat() if published else None,
        "news_after_cutoff": after_cutoff,
        "nvidia_or_huang_news_count": len(nvidia_hits),
        "nvidia_or_huang_news_titles": nvidia_hits[:5],
    }


def find_event(snapshot: Dict[str, Any], event_id: str) -> Optional[Dict[str, Any]]:
    for cluster in snapshot.get("clusters") or []:
        for event in cluster.get("events") or []:
            if str(event.get("event_id")) == event_id:
                meta = event.get("event_meta") or {}
                entities = meta.get("entities") or {}
                return {
                    "event_id": event_id,
                    "cluster_id": cluster["cluster_id"],
                    "headline": event.get("headline"),
                    "event_time": event.get("event_time"),
                    "first_seen_at": event.get("first_seen_at"),
                    "source_id": event.get("source_id"),
                    "source_tier": event.get("source_tier"),
                    "status": event.get("status"),
                    "materiality": meta.get("materiality"),
                    "entities_symbols": list(entities.get("symbols") or []),
                    "visible": True,
                }
    return None


def _snapshot_event_ids(snapshot: Dict[str, Any]) -> set[str]:
    return {
        str(event.get("event_id"))
        for cluster in snapshot.get("clusters") or []
        for event in cluster.get("events") or []
    }


def collect_post_cutoff_probes(
    db_path: Path,
    *,
    cutoff: str,
    horizon_hours: int = 3,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    """Fetch events first seen *after* cutoff; they must never appear in the snapshot."""
    cutoff_dt = parse_timestamp(cutoff, require_timezone=True)
    until = iso_utc(cutoff_dt + timedelta(hours=horizon_hours))
    records = read_event_records(db_path, since=cutoff, until=until)
    later = sorted(
        (record for record in records if parse_timestamp(record.first_seen_at) > cutoff_dt),
        key=lambda record: (record.first_seen_at, record.event.event_id),
    )
    related = [record for record in later if _nvidia_pattern().search(record.event.headline or "")]
    selected = (related or later)[:limit]
    return [
        {
            "event_id": record.event.event_id,
            "headline": record.event.headline,
            "first_seen_at": record.first_seen_at,
            "source_id": record.event.source_id,
        }
        for record in selected
    ]


def symbol_coverage(snapshot: Dict[str, Any], symbols: Sequence[str]) -> Dict[str, Any]:
    rows: Dict[str, Any] = {}
    for symbol in symbols:
        matches = []
        for cluster in snapshot.get("clusters") or []:
            headline = str(cluster.get("headline") or "")
            if symbol_mentioned(headline, symbol):
                matches.append({
                    "cluster_id": cluster["cluster_id"],
                    "first_seen_at": cluster["first_seen_at"],
                    "headline": headline[:180],
                    "event_ids": list(cluster.get("event_ids") or []),
                    "materiality": cluster.get("materiality"),
                })
        rows[symbol] = {
            "direct_headline_cluster_count": len(matches),
            "direct_headline_event_count": sum(len(row["event_ids"]) for row in matches),
            "sample_clusters": matches[:10],
        }
    sector_clusters = [
        cluster for cluster in snapshot.get("clusters") or []
        if sector_mentioned(str(cluster.get("headline") or ""))
    ]
    return {
        "symbols": rows,
        "sector_context": {
            "cluster_count": len(sector_clusters),
            "event_count": sum(len(cluster.get("event_ids") or []) for cluster in sector_clusters),
            "sample_clusters": [
                {
                    "cluster_id": cluster["cluster_id"],
                    "first_seen_at": cluster["first_seen_at"],
                    "headline": str(cluster.get("headline") or "")[:180],
                }
                for cluster in sector_clusters[:10]
            ],
        },
    }


def evaluate_case(
    *,
    snapshot: Dict[str, Any],
    cutoff: str,
    key_event_ids: Sequence[str],
    post_cutoff_probe_ids: Sequence[str],
    brief_context: Dict[str, Any],
    prior_snapshot: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    cutoff_dt = parse_timestamp(cutoff, require_timezone=True)
    events = [
        event for cluster in snapshot.get("clusters") or [] for event in cluster.get("events") or []
    ]
    visible_ids = _snapshot_event_ids(snapshot)
    first_seen = [
        parse_timestamp(str(event["first_seen_at"]), require_timezone=True)
        for event in events
        if event.get("first_seen_at")
    ]
    key_entries = [entry for entry in (find_event(snapshot, event_id) for event_id in key_event_ids) if entry]
    selected_ids = set((brief_context.get("selection") or {}).get("selected_cluster_ids") or [])
    checks = {
        "snapshot_nonempty": bool(events),
        "snapshot_grounded": not validate_event_snapshot(snapshot),
        "no_future_events": len(first_seen) == len(events) and all(when <= cutoff_dt for when in first_seen),
        "key_events_declared": bool(key_event_ids),
        "key_events_visible": len(key_entries) == len(key_event_ids) and all(
            entry["visible"] for entry in key_entries
        ),
        "key_events_selected_in_brief": len(key_entries) == len(key_event_ids) and all(
            entry["cluster_id"] in selected_ids for entry in key_entries
        ),
        "post_cutoff_probes_declared": bool(post_cutoff_probe_ids),
        "post_cutoff_probes_excluded": all(
            event_id not in visible_ids for event_id in post_cutoff_probe_ids
        ),
    }
    if prior_snapshot is not None:
        prior_ids = _snapshot_event_ids(prior_snapshot)
        checks["key_events_absent_at_prior_cutoff"] = all(
            event_id not in prior_ids for event_id in key_event_ids
        )
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "key_event_entries": key_entries,
        "key_events_missing": [
            event_id for event_id in key_event_ids if find_event(snapshot, event_id) is None
        ],
    }


def _build_snapshot(
    db_path: Path,
    *,
    as_of: str,
    lookback_hours: int,
    expected_sources: Sequence[str],
) -> tuple[Dict[str, Any], str, str]:
    as_of_dt = parse_timestamp(as_of, require_timezone=True)
    since = iso_utc(as_of_dt - timedelta(hours=lookback_hours))
    records = read_event_records(db_path, since=since, until=as_of)
    snapshot = build_event_snapshot(
        records,
        as_of=as_of,
        since=since,
        source_health=read_source_health(db_path),
        expected_sources=expected_sources,
    )
    return snapshot, since, as_of


def _priority_cluster_ids(snapshot: Dict[str, Any], event_ids: Sequence[str]) -> List[str]:
    wanted = {str(event_id) for event_id in event_ids}
    return sorted({
        cluster["cluster_id"]
        for cluster in snapshot.get("clusters") or []
        if wanted.intersection(str(item) for item in cluster.get("event_ids") or [])
    })


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Freeze an offline Event Intelligence as-of case")
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "event_stream.db")
    parser.add_argument("--as-of", required=True, help="case cutoff, timezone required")
    parser.add_argument("--prior-as-of", default="", help="optional earlier checkpoint (negative control)")
    parser.add_argument("--lookback-hours", type=int, default=24)
    parser.add_argument("--raw-root", type=Path, required=True)
    parser.add_argument("--symbols", required=True, help="comma separated, e.g. NVDA,AMD,...")
    parser.add_argument("--target-symbols", default="",
                        help="target-aware brief universe; defaults to --symbols when supplemental is on")
    parser.add_argument("--key-event-ids", default="", help="comma separated event ids that must be visible")
    parser.add_argument("--post-cutoff-horizon-hours", type=int, default=3)
    parser.add_argument("--brief-limit", type=int, default=48)
    parser.add_argument("--supplemental-raw-root", type=Path,
                        help="optional: extract supplemental evidence from DA raw checkpoints")
    parser.add_argument("--supplemental-cutoff", default="",
                        help="supplemental cutoff; must equal --as-of (shared cutoff)")
    parser.add_argument("--supplemental-symbols", default="",
                        help="supplemental symbols; default: --symbols")
    parser.add_argument("--supplemental-limit", type=int, default=0,
                        help="max supplemental facts merged; 0=all")
    parser.add_argument("--retained-case-dir", type=Path,
                        default=ROOT / "data" / "shadow" / "event_intelligence" / "nvda_20261112_v1",
                        help="optional earlier case dir to record as retained")
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.lookback_hours <= 0:
        raise SystemExit("--lookback-hours must be positive")
    if args.brief_limit <= 0:
        raise SystemExit("--brief-limit must be positive")
    if args.output_dir.exists():
        raise SystemExit(f"输出目录已存在, 拒绝覆盖: {args.output_dir}")
    if not args.db.exists():
        raise SystemExit(f"event store missing: {args.db}")

    symbols = [item.strip().upper().removeprefix("US.") for item in args.symbols.split(",") if item.strip()]
    if not symbols:
        raise SystemExit("--symbols is empty")
    key_event_ids = [item.strip() for item in args.key_event_ids.split(",") if item.strip()]
    if not key_event_ids:
        raise SystemExit("--key-event-ids is required for a frozen case")

    cutoff = iso_utc(parse_timestamp(args.as_of, require_timezone=True))
    cutoff_dt = parse_timestamp(cutoff, require_timezone=True)
    cutoff_et = cutoff_dt.astimezone(ET)
    expected_sources = sorted(default_source_registry())

    snapshot, since, cutoff = _build_snapshot(
        args.db, as_of=cutoff, lookback_hours=args.lookback_hours,
        expected_sources=expected_sources,
    )
    priority_cluster_ids = _priority_cluster_ids(snapshot, key_event_ids)
    target_symbols = [
        item.strip().upper().removeprefix("US.")
        for item in args.target_symbols.split(",") if item.strip()
    ]
    if not target_symbols and args.supplemental_raw_root:
        target_symbols = list(symbols)
    brief_context = build_brief_context(
        snapshot, limit=args.brief_limit, priority_cluster_ids=priority_cluster_ids,
        target_universe=target_symbols,
    )
    supplemental = None
    supplemental_cutoff_et = None
    supplemental_symbols: List[str] = []
    if args.supplemental_raw_root:
        if not args.supplemental_cutoff:
            raise SystemExit("--supplemental-cutoff is required with --supplemental-raw-root")
        supplemental_cutoff = iso_utc(parse_timestamp(args.supplemental_cutoff, require_timezone=True))
        if supplemental_cutoff != cutoff:
            raise SystemExit(
                f"shared cutoff mismatch: --as-of={cutoff} --supplemental-cutoff={supplemental_cutoff}"
            )
        supplemental_symbols = [
            item.strip().upper().removeprefix("US.")
            for item in (args.supplemental_symbols or args.symbols).split(",") if item.strip()
        ] or list(CASE_SYMBOLS)
        supplemental = extract_supplemental_evidence(
            args.supplemental_raw_root,
            cutoff=supplemental_cutoff,
            case_symbols=supplemental_symbols,
        )
        brief_context = merge_supplemental_into_context(
            brief_context, supplemental,
            cutoff=supplemental_cutoff,
            limit=args.supplemental_limit,
            case_symbols=supplemental_symbols,
        )
        supplemental_cutoff_et = supplemental["cutoff_et"]

    prior_snapshot = None
    prior_since = None
    if args.prior_as_of:
        prior_snapshot, prior_since, _ = _build_snapshot(
            args.db, as_of=args.prior_as_of, lookback_hours=args.lookback_hours,
            expected_sources=expected_sources,
        )

    post_cutoff_probes = collect_post_cutoff_probes(
        args.db, cutoff=cutoff, horizon_hours=args.post_cutoff_horizon_hours,
    )
    evaluation = evaluate_case(
        snapshot=snapshot,
        cutoff=cutoff,
        key_event_ids=key_event_ids,
        post_cutoff_probe_ids=[row["event_id"] for row in post_cutoff_probes],
        brief_context=brief_context,
        prior_snapshot=prior_snapshot,
    )

    checkpoints = []
    checkpoint_errors = []
    session_date = cutoff_et.date().isoformat()
    for symbol in symbols:
        raw_path = args.raw_root / f"US.{symbol}.raw.json"
        if not raw_path.exists():
            checkpoint_errors.append(f"missing_raw_checkpoint:{symbol}:{raw_path}")
            continue
        entry = checkpoint_entry(raw_path, symbol=f"US.{symbol}", cutoff=cutoff_dt)
        if entry.get("trade_date") != session_date:
            checkpoint_errors.append(
                f"trade_date_mismatch:{symbol}:{entry.get('trade_date')}!={session_date}"
            )
        checkpoints.append(entry)

    coverage = symbol_coverage(snapshot, symbols)
    selected_ids = set(brief_context["selection"]["selected_cluster_ids"])
    for symbol, row in coverage["symbols"].items():
        row["selected_brief_cluster_count"] = sum(
            1 for cluster in row["sample_clusters"] if cluster["cluster_id"] in selected_ids
        )
    coverage["sector_context"]["selected_brief_cluster_count"] = sum(
        1 for cluster in snapshot.get("clusters") or []
        if cluster["cluster_id"] in selected_ids
        and sector_mentioned(str(cluster.get("headline") or ""))
    )

    supplemental_coverage = None
    supplemental_errors: List[str] = []
    if supplemental is not None:
        omitted_ids = set(
            (brief_context.get("supplemental") or {}).get("omitted_evidence_ids") or []
        )
        merged_ids = {
            item["evidence_id"] for item in supplemental["items"]
            if item["evidence_id"] not in omitted_ids
        }
        supplemental_errors = validate_supplemental_evidence(
            supplemental, cutoff=cutoff, case_symbols=supplemental_symbols,
        )
        symbol_rows = {}
        for symbol in supplemental_symbols:
            source_items = [
                item for item in supplemental["items"]
                if f"US.{symbol}" in item["source_symbols"]
            ]
            mentioned_items = [
                item for item in supplemental["items"]
                if symbol in item["mentioned_symbols"]
            ]
            symbol_rows[f"US.{symbol}"] = {
                "source_checkpoint_item_count": len(source_items),
                "mentioned_item_count": len(mentioned_items),
                "selected_fact_count": sum(
                    1 for item in supplemental["items"]
                    if item["evidence_id"] in merged_ids
                    and (f"US.{symbol}" in item["source_symbols"] or symbol in item["mentioned_symbols"])
                ),
                "nvda_related_item_count": sum(1 for item in mentioned_items if item["nvda_related"]),
                "date_only_item_count": sum(
                    1 for item in mentioned_items
                    if item["event_time_precision"] == "DATE_ONLY"
                ),
                "sample_items": [
                    {
                        "evidence_id": item["evidence_id"],
                        "headline": item["headline"],
                        "event_time": item["event_time"],
                        "event_time_precision": item["event_time_precision"],
                        "first_seen_at": item["first_seen_at"],
                        "source_symbols": item["source_symbols"],
                        "mentioned_symbols": item["mentioned_symbols"],
                        "source_tier": item["source_tier"],
                    }
                    for item in mentioned_items[:5]
                ],
            }
        supplemental_coverage = {
            "cutoff": supplemental["cutoff"],
            "cutoff_et": supplemental["cutoff_et"],
            "total_evidence_count": len(supplemental["items"]),
            "merged_fact_count": len(merged_ids),
            "omitted_fact_count": len(supplemental["items"]) - len(merged_ids),
            "stats": supplemental["stats"],
            "symbols": symbol_rows,
            "date_only_items": [
                {
                    "evidence_id": item["evidence_id"],
                    "headline": item["headline"],
                    "event_time": item["event_time"],
                    "event_time_precision": item["event_time_precision"],
                    "first_seen_at": item["first_seen_at"],
                }
                for item in supplemental["items"]
                if item["event_time_precision"] == "DATE_ONLY"
            ],
            "rejected": supplemental["rejected"],
        }
        coverage["supplemental"] = supplemental_coverage

    checks = dict(evaluation["checks"])
    checks["raw_checkpoints_present"] = not checkpoint_errors
    checks["raw_checkpoints_trade_date_match"] = not any(
        item.startswith("trade_date_mismatch:") for item in checkpoint_errors
    )
    priority_unknown = brief_context["selection"]["requested_priority_unknown_cluster_ids"]
    checks["priority_event_ids_resolved"] = not priority_unknown
    if supplemental is not None:
        checks["raw_user_prompts_recorded"] = all(
            entry.get("user_prompt_sha256") for entry in checkpoints
        )
        checks["supplemental_shared_cutoff"] = supplemental["cutoff"] == cutoff
        checks["supplemental_grounded"] = not supplemental_errors
        checks["supplemental_no_future"] = (
            supplemental["stats"]["rejected_future_count"] == 0
            and not any(row.get("reason") == "future_publish_or_collection"
                        for row in supplemental["rejected"])
        )
        checks["supplemental_nonempty"] = len(supplemental["items"]) > 0
        checks["supplemental_selected"] = (
            (brief_context.get("supplemental") or {}).get("merged_fact_count", 0) > 0
        )
        checks["supplemental_date_only_has_no_clock"] = all(
            "T" not in str(row.get("event_time"))
            for row in supplemental_coverage["date_only_items"]
        )
    passed = all(checks.values())

    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=False)
    snapshot_name = f"{cutoff.replace(':', '').replace('+', 'p')}.json"
    snapshot_path = output_dir / "snapshots" / snapshot_name
    _write_json_new(snapshot_path, snapshot)
    prior_path = None
    if prior_snapshot is not None:
        prior_cutoff = iso_utc(parse_timestamp(args.prior_as_of, require_timezone=True))
        prior_name = f"prior_{prior_cutoff.replace(':', '').replace('+', 'p')}.json"
        prior_path = output_dir / "snapshots" / prior_name
        _write_json_new(prior_path, prior_snapshot)
    brief_path = output_dir / "brief_context.json"
    _write_json_new(brief_path, brief_context)
    coverage_path = output_dir / "symbol_coverage.json"
    _write_json_new(coverage_path, coverage)
    supplemental_path = None
    if supplemental is not None:
        supplemental_path = output_dir / "supplemental_evidence.json"
        _write_json_new(supplemental_path, supplemental)

    facts = brief_context["facts"]
    facts_with_urls = sum(bool(fact.get("source_urls")) for fact in facts)
    facts_with_members = sum(len(fact.get("member_facts") or []) > 1 for fact in facts)
    key_entities_empty = [
        entry["event_id"] for entry in evaluation["key_event_entries"]
        if not entry.get("entities_symbols")
    ]
    raw_news_after_cutoff = [
        {"symbol": entry["symbol"], **row}
        for entry in checkpoints for row in entry.get("news_after_cutoff") or []
    ]
    if supplemental is not None:
        cutoff_justification = (
            f"{cutoff} = {cutoff_et.isoformat()}, after the last raw checkpoint collection "
            f"({max((entry['collected_at'] for entry in checkpoints), default='n/a')}); "
            "shared by the EventStore snapshot and the supplemental raw evidence."
        )
    else:
        cutoff_justification = (
            "13:15 UTC = 08:15 ET (EST), the DEB §65.1 DA premarket batch time; "
            "it precedes every raw checkpoint saved_at and every post-cutoff probe."
        )
    retained_case = None
    if args.retained_case_dir and Path(args.retained_case_dir).exists():
        retained_dir = Path(args.retained_case_dir)
        retained_manifest = retained_dir / "case_manifest.json"
        retained_sums = retained_dir / "SHA256SUMS.txt"
        retained_case = {
            "path": _relative(retained_dir),
            "case_manifest_sha256": _sha256_file(retained_manifest) if retained_manifest.exists() else None,
            "sha256sums_sha256": _sha256_file(retained_sums) if retained_sums.exists() else None,
        }

    manifest = {
        "schema": "event_intelligence_case_manifest_v1",
        "case_id": output_dir.name,
        "mode": "R0_OFFLINE",
        "generated_at": datetime.now(UTC).isoformat(),
        "as_of": cutoff,
        "as_of_et": cutoff_et.isoformat(),
        "since": since,
        "lookback_hours": args.lookback_hours,
        "cutoff_justification": cutoff_justification,
        "retained_prior_case": retained_case,
        "symbols": [f"US.{symbol}" for symbol in symbols],
        "db": {"path": str(args.db), "mode": "read-only", "uri_suffix": "?mode=ro"},
        "snapshot": {
            "path": _relative(snapshot_path),
            "sha256": _sha256_file(snapshot_path),
            "snapshot_id": snapshot["snapshot_id"],
            "event_hash": snapshot["event_hash"],
            "event_count": snapshot["event_count"],
            "cluster_count": snapshot["cluster_count"],
            "degraded": snapshot["degraded"],
            "unknowns": snapshot["unknowns"],
            "source_event_counts": snapshot["source_event_counts"],
            "latency": snapshot["latency"],
        },
        "prior_checkpoint": None if prior_snapshot is None else {
            "as_of": iso_utc(parse_timestamp(args.prior_as_of, require_timezone=True)),
            "since": prior_since,
            "path": _relative(prior_path) if prior_path else None,
            "sha256": _sha256_file(prior_path) if prior_path else None,
            "snapshot_id": prior_snapshot["snapshot_id"],
            "event_count": prior_snapshot["event_count"],
            "cluster_count": prior_snapshot["cluster_count"],
            "key_events_visible": [
                event_id for event_id in key_event_ids
                if find_event(prior_snapshot, event_id) is not None
            ],
        },
        "event_visibility": {
            "key_event_ids": key_event_ids,
            "key_events": [
                {
                    **entry,
                    "selected_in_brief": entry["cluster_id"] in selected_ids,
                }
                for entry in evaluation["key_event_entries"]
            ],
            "key_events_missing": evaluation["key_events_missing"],
            "priority_cluster_ids": priority_cluster_ids,
            "post_cutoff_probe_events": [
                {
                    **row,
                    "excluded_from_snapshot": row["event_id"] not in _snapshot_event_ids(snapshot),
                }
                for row in post_cutoff_probes
            ],
        },
        "raw_checkpoints": checkpoints,
        "raw_news_after_cutoff": raw_news_after_cutoff,
        "raw_checkpoint_errors": checkpoint_errors,
        "supplemental_evidence": None if supplemental is None else {
            "schema": supplemental["schema"],
            "cutoff": supplemental["cutoff"],
            "cutoff_et": supplemental["cutoff_et"],
            "raw_root": _relative(args.supplemental_raw_root),
            "symbols": [f"US.{symbol}" for symbol in supplemental_symbols],
            "file": {
                "path": _relative(supplemental_path),
                "sha256": _sha256_file(supplemental_path),
                "evidence_hash": supplemental["evidence_hash"],
            },
            "stats": supplemental["stats"],
            "grounding_errors": supplemental_errors,
            "rejected": supplemental["rejected"],
            "coverage": supplemental_coverage,
            "date_only_policy": (
                "DATE_ONLY items keep event_time as YYYY-MM-DD and use the actual raw "
                "collection time as first_seen_at; no midnight timestamp is fabricated."
            ),
            "stable_id_policy": (
                "SUP-<sha256(normalized title or URL + event date)[:16]>; duplicates "
                "across checkpoints keep one evidence id and all member facts."
            ),
        },
        "quality_metrics": {
            "brief_selected_fact_count": len(facts),
            "brief_direct_fact_count": len(brief_context["facts"]) - (
                (brief_context.get("supplemental") or {}).get("merged_fact_count", 0)
            ),
            "brief_supplemental_fact_count": (
                (brief_context.get("supplemental") or {}).get("merged_fact_count", 0)
            ),
            "brief_omitted_cluster_count": brief_context["selection"]["omitted_cluster_count"],
            "brief_omitted_event_count": brief_context["selection"]["omitted_event_count"],
            "facts_with_source_urls": facts_with_urls,
            "facts_with_multiple_member_facts": facts_with_members,
            "direct_headline_coverage": {
                symbol: row["direct_headline_event_count"]
                for symbol, row in coverage["symbols"].items()
            },
            "supplemental_coverage": (
                None if supplemental_coverage is None else {
                    symbol: {
                        "source_checkpoint_item_count": row["source_checkpoint_item_count"],
                        "mentioned_item_count": row["mentioned_item_count"],
                        "selected_fact_count": row["selected_fact_count"],
                        "nvda_related_item_count": row["nvda_related_item_count"],
                        "date_only_item_count": row["date_only_item_count"],
                    }
                    for symbol, row in supplemental_coverage["symbols"].items()
                }
            ),
            "supplemental_total_evidence_count": (
                None if supplemental is None else len(supplemental["items"])
            ),
            "supplemental_duplicates_collapsed": (
                None if supplemental is None else supplemental["stats"]["duplicates_collapsed"]
            ),
            "supplemental_date_only_item_count": (
                None if supplemental is None else supplemental["stats"]["date_only_item_count"]
            ),
            "supplemental_unknown_time_item_count": (
                None if supplemental is None else supplemental["stats"]["unknown_time_item_count"]
            ),
            "sector_context_cluster_count": coverage["sector_context"]["cluster_count"],
            "sector_context_selected_cluster_count": coverage["sector_context"]["selected_brief_cluster_count"],
            "key_event_entities_symbols_empty": key_entities_empty,
            "transmission_metrics": "not_available_until_paid_LLM_brief",
            "llm_calls": 0,
        },
        "checks": checks,
        "passed": passed,
        "artifacts": {
            "snapshot": {"path": _relative(snapshot_path), "sha256": _sha256_file(snapshot_path)},
            "prior_snapshot": (
                {"path": _relative(prior_path), "sha256": _sha256_file(prior_path)}
                if prior_path else None
            ),
            "brief_context": {"path": _relative(brief_path), "sha256": _sha256_file(brief_path)},
            "symbol_coverage": {"path": _relative(coverage_path), "sha256": _sha256_file(coverage_path)},
            "supplemental_evidence": (
                {
                    "path": _relative(supplemental_path),
                    "sha256": _sha256_file(supplemental_path),
                } if supplemental_path else None
            ),
        },
        "known_limitations": [
            "source_health rows are current, not a historical as-of ledger; degraded=true is expected (DEB §17.3).",
            "EventStore entity tagging is incomplete; symbol coverage above uses headline text rules, not entity tables.",
            "Direct peer-symbol headlines are sparse; peer impact must be judged from LLM transmissions after the paid brief run.",
            (
                "Supplemental evidence is extracted from raw DA checkpoints, not EventStore; "
                "its first_seen_at is the actual raw collection time, not an ingestion timestamp."
                if supplemental is not None else
                "Some per-symbol raw checkpoints were collected after the shared cutoff; their post-cutoff company news is recorded in raw_checkpoints and is a control-arm asymmetry."
            ),
            "Dedupe is exact URL/title only; semantically duplicated headlines can remain separate clusters.",
            (
                "Date-only supplemental items keep event_time as YYYY-MM-DD and first_seen_at as "
                "the raw collection time; no midnight clock is inferred."
                if supplemental is not None else
                "No supplemental raw-checkpoint evidence was merged in this artifact."
            ),
            "No LLM / paid call / production DB write is part of this artifact.",
        ],
        "production_writes": False,
        "notifier_calls": 0,
        "llm_calls": 0,
    }
    manifest_path = output_dir / "case_manifest.json"
    _write_json_new(manifest_path, manifest)

    sums = []
    for path in sorted(output_dir.rglob("*")):
        if path.is_file() and path.name != "SHA256SUMS.txt":
            sums.append(f"{_sha256_file(path)}  {path.relative_to(output_dir)}")
    (output_dir / "SHA256SUMS.txt").write_text("\n".join(sums) + "\n", encoding="utf-8")

    print(json.dumps({
        "case_id": manifest["case_id"],
        "as_of": cutoff,
        "as_of_et": cutoff_et.isoformat(),
        "snapshot_id": snapshot["snapshot_id"],
        "event_count": snapshot["event_count"],
        "cluster_count": snapshot["cluster_count"],
        "key_events": [
            {"event_id": item["event_id"], "selected_in_brief": item["selected_in_brief"]}
            for item in manifest["event_visibility"]["key_events"]
        ],
        "post_cutoff_probe_count": len(post_cutoff_probes),
        "supplemental_total_evidence_count": (
            None if supplemental is None else len(supplemental["items"])
        ),
        "supplemental_merged_fact_count": (
            None if supplemental is None else (brief_context.get("supplemental") or {}).get("merged_fact_count")
        ),
        "supplemental_stats": None if supplemental is None else supplemental["stats"],
        "checks": checks,
        "passed": passed,
        "output_dir": str(output_dir),
        "llm_calls": 0,
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())