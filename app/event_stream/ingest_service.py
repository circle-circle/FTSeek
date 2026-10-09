from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Dict, Iterable, List, Optional

from app.event_stream.models import SourceResult
from app.event_stream.source_registry import default_source_registry
from app.event_stream.store import EventStore


class IngestService:
    def __init__(
        self,
        store: EventStore,
        sources: Optional[Dict[str, Callable[[], SourceResult]]] = None,
    ):
        self.store = store
        self.sources = sources or default_source_registry()

    def ingest(self, source_ids: Optional[Iterable[str]] = None) -> Dict[str, dict]:
        requested = set(source_ids) if source_ids else set(self.sources)
        summary: Dict[str, dict] = {}
        for source_id in sorted(requested):
            fetcher = self.sources.get(source_id)
            if fetcher is None:
                summary[source_id] = {
                    "status": "ERROR",
                    "error": "source not registered",
                }
                continue
            result = fetcher()
            if result.raw_event is not None:
                self.store.append_raw_event(result.raw_event)
            inserted_events = 0
            first_seen_at = result.raw_event.fetched_at if result.raw_event else None
            for event in result.events:
                if self.store.append_canonical_event(event, first_seen_at=first_seen_at):
                    inserted_events += 1
            now = datetime.now(timezone.utc).isoformat()
            if result.status == "OK":
                self.store.mark_source_health(
                    source_id,
                    "OK",
                    result.latency_ms,
                    last_success_at=now,
                    last_error=None,
                )
            else:
                self.store.mark_source_health(
                    source_id,
                    "ERROR",
                    result.latency_ms,
                    last_success_at=None,
                    last_error=result.error,
                )
            summary[source_id] = {
                "status": result.status,
                "latency_ms": result.latency_ms,
                "raw_inserted": result.raw_event is not None,
                "events_seen": len(result.events),
                "events_inserted": inserted_events,
                "error": result.error,
            }
        return summary
