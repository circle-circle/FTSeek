from __future__ import annotations

from pathlib import Path
from typing import Iterable, List, Optional

from app.event_stream.dedupe import deduplicate_events
from app.event_stream.relevance import filter_relevant_events, load_relevance_context

from app.event_stream.models import CanonicalEvent
from app.event_stream.store import EventStore


def build_clue_text(events: Iterable[CanonicalEvent], limit: int = 20) -> str:
    lines = []
    for event in list(events)[:limit]:
        event_time = event.event_time or "unknown-time"
        source_tier = event.source_tier or "T?"
        event_type = event.event_type or "UNKNOWN"
        headline = event.headline.strip()
        source_url = event.source_url or ""
        line = f"[{source_tier}/{event_type}] {event_time} | {headline}"
        if source_url:
            line += f" | source={source_url}"
        lines.append(line)
    if not lines:
        return ""
    return "\n".join(lines)


def gather_event_stream_clue(
    store_path: Optional[Path] = None,
    limit: int = 20,
    relevant_only: bool = False,
    dedupe: bool = False,
    scan_limit: int = 500,
) -> str:
    store = EventStore(store_path)
    events = store.get_recent_canonical_events(limit=scan_limit)
    if relevant_only:
        events = filter_relevant_events(events, load_relevance_context())
    if dedupe:
        events = [cluster.primary for cluster in deduplicate_events(events)]
    return build_clue_text(events, limit=limit)
