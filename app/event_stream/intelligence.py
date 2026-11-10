from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from app.event_stream.dedupe import deduplicate_events_exact
from app.event_stream.event_schema import build_event_meta
from app.event_stream.models import CanonicalEvent

UTC = timezone.utc
RETRACTED_STATUSES = frozenset({"RETRACTED", "WITHDRAWN", "CORRECTED"})


def parse_timestamp(value: str, *, require_timezone: bool = False) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        if require_timezone:
            raise ValueError(f"timezone required: {value!r}")
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _json_object(raw: Optional[str], field_name: str) -> Tuple[Dict[str, Any], List[str]]:
    if not raw:
        return {}, []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}, [f"invalid_{field_name}"]
    if not isinstance(value, dict):
        return {}, [f"non_object_{field_name}"]
    return value, []


@dataclass(frozen=True)
class EventRecord:
    event: CanonicalEvent
    first_seen_at: str
    published_to_seen_ms: Optional[int]
    parse_warnings: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        payload = self.event.to_dict()
        payload.update({
            "first_seen_at": self.first_seen_at,
            "published_to_seen_ms": self.published_to_seen_ms,
            "parse_warnings": list(self.parse_warnings),
        })
        return payload


def read_event_records(
    db_path: Path,
    *,
    since: str,
    until: str,
) -> List[EventRecord]:
    since_utc = iso_utc(parse_timestamp(since, require_timezone=True))
    until_utc = iso_utc(parse_timestamp(until, require_timezone=True))
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        rows = conn.execute(
            """SELECT event_id, source_id, source_tier, source_url, event_type,
                      event_time, headline, summary, facts_json, status, confidence,
                      reasoning_version, event_meta_json, first_seen_at,
                      published_to_seen_ms
               FROM canonical_events
               WHERE first_seen_at IS NOT NULL
                 AND first_seen_at >= ? AND first_seen_at <= ?
               ORDER BY first_seen_at, event_id""",
            (since_utc, until_utc),
        ).fetchall()
    records = []
    for row in rows:
        facts, fact_warnings = _json_object(row[8], "facts_json")
        event_meta, meta_warnings = _json_object(row[12], "event_meta_json")
        event = CanonicalEvent(
            event_id=row[0], source_id=row[1], source_tier=row[2], source_url=row[3],
            event_type=row[4], event_time=row[5], headline=row[6], summary=row[7] or "",
            facts=facts, status=row[9], confidence=float(row[10]),
            reasoning_version=row[11], event_meta=event_meta,
        )
        first_seen_at = iso_utc(parse_timestamp(row[13]))
        if not since_utc <= first_seen_at <= until_utc:
            continue
        records.append(EventRecord(
            event=event,
            first_seen_at=first_seen_at,
            published_to_seen_ms=row[14],
            parse_warnings=tuple(fact_warnings + meta_warnings),
        ))
    return records


def read_source_health(db_path: Path) -> List[Dict[str, Any]]:
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        rows = conn.execute(
            """SELECT source_id, status, latency_ms, last_success_at, last_error
               FROM source_health ORDER BY source_id"""
        ).fetchall()
    return [
        {
            "source_id": row[0], "status": row[1], "latency_ms": row[2],
            "last_success_at": row[3], "last_error": row[4],
        }
        for row in rows
    ]


def record_fingerprint(record: EventRecord) -> str:
    canonical = json.dumps(
        record.event.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def select_visible_records(
    records: Iterable[EventRecord],
    *,
    as_of: str,
    since: Optional[str] = None,
) -> List[EventRecord]:
    upper = parse_timestamp(as_of, require_timezone=True)
    lower = parse_timestamp(since, require_timezone=True) if since else None
    latest: Dict[str, EventRecord] = {}
    for record in records:
        first_seen = parse_timestamp(record.first_seen_at)
        if first_seen > upper or (lower is not None and first_seen < lower):
            continue
        previous = latest.get(record.event.event_id)
        if previous is not None:
            previous_time = parse_timestamp(previous.first_seen_at)
            if first_seen == previous_time and record_fingerprint(previous) != record_fingerprint(record):
                raise ValueError(f"conflicting revisions at one first_seen_at: {record.event.event_id}")
            if first_seen < previous_time:
                continue
        latest[record.event.event_id] = record
    return sorted(
        latest.values(),
        key=lambda item: (item.first_seen_at, item.event.event_id),
    )


def _percentile(values: Sequence[float], quantile: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        result = ordered[lower]
    else:
        result = ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
    return round(float(result), 1)


def _latency_row(records: Sequence[EventRecord]) -> Dict[str, Any]:
    supplied = [item.published_to_seen_ms for item in records if item.published_to_seen_ms is not None]
    valid = [float(value) for value in supplied if value >= 0]
    over_60m = sum(value > 60 * 60 * 1000 for value in valid)
    return {
        "unit": "milliseconds",
        "event_count": len(records),
        "valid_latency_count": len(valid),
        "missing_latency_count": len(records) - len(supplied),
        "negative_latency_count": len(supplied) - len(valid),
        "p50_ms": _percentile(valid, 0.50),
        "p90_ms": _percentile(valid, 0.90),
        "max_ms": max(valid) if valid else None,
        "over_60m_count": over_60m,
        "over_60m_ratio_of_valid": round(over_60m / len(valid), 6) if valid else None,
    }


def latency_summary(records: Sequence[EventRecord]) -> Dict[str, Any]:
    by_source: Dict[str, List[EventRecord]] = {}
    for record in records:
        by_source.setdefault(record.event.source_id, []).append(record)
    return {
        "ALL": _latency_row(records),
        "by_source": {
            source_id: _latency_row(by_source[source_id])
            for source_id in sorted(by_source)
        },
    }


def _publisher(event: CanonicalEvent) -> str:
    supplied = str(event.facts.get("source") or "").strip().casefold()
    if supplied:
        return supplied
    host = urlsplit(event.source_url).netloc.casefold().removeprefix("www.")
    return host or event.source_id.casefold()


def _event_hash(records: Sequence[EventRecord]) -> str:
    payload = [item.to_dict() for item in records]
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _source_health_as_of(
    rows: Sequence[Dict[str, Any]],
    *,
    expected_sources: Iterable[str],
    record_sources: Iterable[str],
    as_of: str,
    stale_minutes: int,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    upper = parse_timestamp(as_of, require_timezone=True)
    by_source = {str(row["source_id"]): dict(row) for row in rows}
    sources = sorted(set(expected_sources) | set(record_sources) | set(by_source))
    output = []
    unknowns = []
    for source_id in sources:
        row = by_source.get(source_id, {})
        last_success_raw = row.get("last_success_at")
        try:
            last_success = parse_timestamp(last_success_raw) if last_success_raw else None
        except ValueError:
            last_success = None
        as_of_valid = last_success is not None and last_success <= upper
        status = str(row.get("status") or "MISSING").upper() if as_of_valid else "UNKNOWN_AS_OF"
        stale = last_success is None or last_success < upper - timedelta(minutes=stale_minutes)
        if stale and as_of_valid and status == "OK":
            status = "STALE"
        output.append({
            "source_id": source_id,
            "status": status,
            "latency_ms": row.get("latency_ms"),
            "last_success_at": last_success_raw,
            "last_error": row.get("last_error") if as_of_valid else None,
            "as_of_valid": as_of_valid,
            "stale": stale,
            "time_basis": "last_success_at; current row is not a historical status ledger",
        })
        if status != "OK" or stale:
            unknowns.append(f"source_health:{source_id}:{status}:stale={str(stale).lower()}")
    return output, unknowns


def build_event_snapshot(
    records: Iterable[EventRecord],
    *,
    as_of: str,
    since: str,
    source_health: Sequence[Dict[str, Any]] = (),
    expected_sources: Iterable[str] = (),
    source_stale_minutes: int = 30,
    supersedes: Optional[str] = None,
    delta: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    as_of_utc = iso_utc(parse_timestamp(as_of, require_timezone=True))
    since_utc = iso_utc(parse_timestamp(since, require_timezone=True))
    visible = select_visible_records(records, as_of=as_of_utc, since=since_utc)
    canonical_events = []
    for record in visible:
        meta = record.event.event_meta or build_event_meta(record.event)
        canonical_events.append(replace(record.event, event_meta=meta))
    record_by_id = {record.event.event_id: record for record in visible}
    event_by_id = {event.event_id: event for event in canonical_events}
    clusters = []
    for cluster in deduplicate_events_exact(canonical_events):
        members = [cluster.primary] + list(cluster.duplicates)
        member_ids = sorted(event.event_id for event in members)
        member_records = [record_by_id[event_id] for event_id in member_ids]
        source_ids = sorted({event.source_id for event in members})
        publishers = sorted({_publisher(event) for event in members})
        corroborated = len(publishers) >= 2
        first_seen_at = min(item.first_seen_at for item in member_records)
        materiality = max(
            float(event_by_id[event_id].event_meta.get("materiality") or 0)
            for event_id in member_ids
        )
        clusters.append({
            "cluster_id": cluster.primary.event_id,
            "event_ids": member_ids,
            "event_time": cluster.primary.event_time,
            "first_seen_at": first_seen_at,
            "headline": cluster.primary.headline,
            "summary": cluster.primary.summary,
            "event_type": cluster.primary.event_type,
            "source_tier": cluster.primary.source_tier,
            "source_ids": source_ids,
            "corroborating_publishers": publishers,
            "corroboration": len(publishers),
            "status": "CORROBORATED" if corroborated else cluster.primary.status,
            "materiality": materiality,
            "matched_on": cluster.matched_on,
            "events": [
                replace(event_by_id[event_id], event_meta=event_by_id[event_id].event_meta).to_dict()
                | {
                    "first_seen_at": record_by_id[event_id].first_seen_at,
                    "published_to_seen_ms": record_by_id[event_id].published_to_seen_ms,
                    "parse_warnings": list(record_by_id[event_id].parse_warnings),
                }
                for event_id in member_ids
            ],
        })
    clusters.sort(key=lambda item: (-item["materiality"], item["first_seen_at"], item["cluster_id"]))
    health, health_unknowns = _source_health_as_of(
        source_health,
        expected_sources=expected_sources,
        record_sources=[item.event.source_id for item in visible],
        as_of=as_of_utc,
        stale_minutes=source_stale_minutes,
    )
    unknowns = set(health_unknowns)
    missing_event_time = sum(not item.event.event_time for item in visible)
    if missing_event_time:
        unknowns.add(f"missing_event_time:{missing_event_time}")
    parse_warning_count = sum(len(item.parse_warnings) for item in visible)
    if parse_warning_count:
        unknowns.add(f"event_parse_warnings:{parse_warning_count}")
    event_hash = _event_hash(visible)
    snapshot_id = hashlib.sha256(f"{as_of_utc}|{event_hash}".encode("utf-8")).hexdigest()[:20]
    return {
        "schema": "event_snapshot_v1",
        "snapshot_id": snapshot_id,
        "mode": "R0_OFFLINE",
        "as_of": as_of_utc,
        "since": since_utc,
        "supersedes": supersedes,
        "event_hash": event_hash,
        "dedupe_mode": "exact_url_title_v1",
        "event_count": len(visible),
        "cluster_count": len(clusters),
        "source_event_counts": dict(sorted(Counter(
            item.event.source_id for item in visible
        ).items())),
        "clusters": clusters,
        "latency": latency_summary(visible),
        "source_health": health,
        "degraded": bool(health_unknowns or parse_warning_count),
        "unknowns": sorted(unknowns),
        "delta": list(delta or []),
        "llm_calls": 0,
    }


@dataclass(frozen=True)
class CursorState:
    highwater_first_seen_at: Optional[str] = None
    highwater_event_id: Optional[str] = None
    last_as_of: Optional[str] = None
    seen_fingerprints: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "highwater_first_seen_at": self.highwater_first_seen_at,
            "highwater_event_id": self.highwater_event_id,
            "last_as_of": self.last_as_of,
            "seen_fingerprints": dict(sorted(self.seen_fingerprints.items())),
        }


def advance_cursor(
    state: CursorState,
    records: Iterable[EventRecord],
    *,
    as_of: str,
    overlap_seconds: int = 300,
) -> Tuple[CursorState, List[Dict[str, Any]]]:
    as_of_utc = iso_utc(parse_timestamp(as_of, require_timezone=True))
    upper = parse_timestamp(as_of_utc)
    highwater = parse_timestamp(state.highwater_first_seen_at) if state.highwater_first_seen_at else None
    overlap_start = highwater - timedelta(seconds=overlap_seconds) if highwater else None
    seen = dict(state.seen_fingerprints)
    deltas = []
    candidate_keys = []
    visible = select_visible_records(records, as_of=as_of_utc)
    for record in visible:
        first_seen = parse_timestamp(record.first_seen_at)
        if first_seen > upper or (overlap_start is not None and first_seen < overlap_start):
            continue
        event_id = record.event.event_id
        fingerprint = record_fingerprint(record)
        previous = seen.get(event_id)
        candidate_keys.append((record.first_seen_at, event_id))
        if previous != fingerprint:
            if previous is None:
                change_type = "NEW"
            elif record.event.status.upper() in RETRACTED_STATUSES:
                change_type = "RETRACTED"
            else:
                change_type = "UPDATED"
            deltas.append({
                "change_type": change_type,
                "previous_fingerprint": previous,
                "fingerprint": fingerprint,
                "event": record.to_dict(),
            })
            seen[event_id] = fingerprint
    next_highwater = state.highwater_first_seen_at
    next_event_id = state.highwater_event_id
    if candidate_keys:
        key = max(candidate_keys)
        if next_highwater is None or key >= (next_highwater, next_event_id or ""):
            next_highwater, next_event_id = key
    next_state = CursorState(
        highwater_first_seen_at=next_highwater,
        highwater_event_id=next_event_id,
        last_as_of=as_of_utc,
        seen_fingerprints=seen,
    )
    return next_state, deltas


def run_event_replay(
    records: Iterable[EventRecord],
    *,
    checkpoints: Sequence[str],
    since: str,
    source_health: Sequence[Dict[str, Any]] = (),
    expected_sources: Iterable[str] = (),
    overlap_seconds: int = 300,
) -> Dict[str, Any]:
    if not checkpoints:
        raise ValueError("at least one checkpoint is required")
    normalized = [iso_utc(parse_timestamp(item, require_timezone=True)) for item in checkpoints]
    if normalized != sorted(set(normalized)):
        raise ValueError("checkpoints must be unique and strictly increasing")
    all_records = list(records)
    state = CursorState()
    snapshots = []
    steps = []
    previous_snapshot_id = None
    for checkpoint in normalized:
        visible = select_visible_records(all_records, as_of=checkpoint, since=since)
        state, deltas = advance_cursor(
            state, visible, as_of=checkpoint, overlap_seconds=overlap_seconds,
        )
        change_counts = Counter(item["change_type"] for item in deltas)
        if previous_snapshot_id is not None and not deltas:
            steps.append({
                "as_of": checkpoint, "status": "NO_CHANGE", "snapshot_index": None,
                "delta_counts": {}, "llm_calls": 0,
            })
            continue
        snapshot = build_event_snapshot(
            visible,
            as_of=checkpoint,
            since=since,
            source_health=source_health,
            expected_sources=expected_sources,
            supersedes=previous_snapshot_id,
            delta=deltas,
        )
        snapshot["replay_sequence"] = len(snapshots) + 1
        snapshots.append(snapshot)
        previous_snapshot_id = snapshot["snapshot_id"]
        steps.append({
            "as_of": checkpoint,
            "status": "SNAPSHOT",
            "snapshot_index": len(snapshots) - 1,
            "snapshot_id": snapshot["snapshot_id"],
            "delta_counts": dict(sorted(change_counts.items())),
            "llm_calls": 0,
        })
    return {
        "schema": "event_replay_v1",
        "mode": "R0_OFFLINE",
        "since": iso_utc(parse_timestamp(since, require_timezone=True)),
        "checkpoints": normalized,
        "steps": steps,
        "snapshots": snapshots,
        "final_cursor": state.to_dict(),
        "llm_calls": 0,
    }
