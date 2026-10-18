from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable, Dict, Iterable, List, Optional

from app.event_stream.event_schema import build_event_meta
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
        self.last_total_latency_ms: Optional[int] = None

    @staticmethod
    def _fetch_one(fetcher: Callable[[], SourceResult]):
        started = time.perf_counter()
        try:
            return fetcher(), None, int((time.perf_counter() - started) * 1000)
        except Exception as exc:
            return None, exc, int((time.perf_counter() - started) * 1000)

    def ingest(self, source_ids: Optional[Iterable[str]] = None) -> Dict[str, dict]:
        started = time.perf_counter()
        requested = set(source_ids) if source_ids else set(self.sources)
        summary: Dict[str, dict] = {}
        # 2026-10-17 审计: 原 sorted(requested) 按字母序执行, SEC T0 排最后;
        #   Google 最坏 81.5s + FR/Finnhub 40s, 可在 cron timeout 120s 前把 SEC 饿死。
        #   按 sources 注册顺序处理结果 (default_source_registry 已把 T0 放最前)。
        source_order = {source_id: index for index, source_id in enumerate(self.sources)}
        ordered = sorted(
            requested, key=lambda sid: (source_order.get(sid, len(source_order)), sid)
        )
        for source_id in ordered:
            if source_id not in self.sources:
                summary[source_id] = {
                    "status": "ERROR",
                    "error": "source not registered",
                }

        # 2026-10-18: 源级并发 — 单源最坏 81.5s 不应串行叠加到 >120s cron timeout.
        #   只并发网络抓取; SQLite 写/健康表更新仍按注册顺序在主线程串行执行.
        fetchable = [sid for sid in ordered if sid in self.sources]
        results = {}
        if fetchable:
            with ThreadPoolExecutor(max_workers=min(4, len(fetchable))) as pool:
                futures = {
                    sid: pool.submit(self._fetch_one, self.sources[sid])
                    for sid in fetchable
                }
                for sid in fetchable:
                    results[sid] = futures[sid].result()

        for source_id in ordered:
            if source_id not in self.sources:
                continue
            result, fetch_error, fetch_latency = results[source_id]
            if fetch_error is not None or result is None:
                error = f"{type(fetch_error).__name__}: {fetch_error}" if fetch_error else "empty result"
                self.store.mark_source_health(
                    source_id,
                    "ERROR",
                    fetch_latency,
                    last_success_at=None,
                    last_error=error[:500],
                )
                summary[source_id] = {
                    "status": "ERROR",
                    "partial": False,
                    "latency_ms": fetch_latency,
                    "raw_inserted": False,
                    "events_seen": 0,
                    "events_inserted": 0,
                    "error": error[:500],
                }
                continue
            if result.raw_event is not None:
                self.store.append_raw_event(result.raw_event)
            inserted_events = 0
            first_seen_at = result.raw_event.fetched_at if result.raw_event else None
            for event in result.events:
                if not event.event_meta:
                    event.event_meta = build_event_meta(event)
                if self.store.append_canonical_event(event, first_seen_at=first_seen_at):
                    inserted_events += 1
            now = datetime.now(timezone.utc).isoformat()
            partial = result.status == "OK" and bool(result.error)
            if result.status == "OK":
                self.store.mark_source_health(
                    source_id,
                    "PARTIAL" if partial else "OK",
                    result.latency_ms,
                    last_success_at=now,
                    last_error=result.error if partial else None,
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
                "status": "PARTIAL" if partial else result.status,
                "partial": partial,
                "latency_ms": result.latency_ms,
                "raw_inserted": result.raw_event is not None,
                "events_seen": len(result.events),
                "events_inserted": inserted_events,
                "error": result.error,
            }
        self.last_total_latency_ms = int((time.perf_counter() - started) * 1000)
        return summary
