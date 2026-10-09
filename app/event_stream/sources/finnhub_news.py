from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import requests

from app.event_stream.models import (
    CanonicalEvent,
    RawEvent,
    SourceResult,
    make_event_id,
    make_raw_id,
)

SOURCE_ID = "FINNHUB_GENERAL_NEWS"
SOURCE_TIER = "T1"
URL = "https://finnhub.io/api/v1/news"
HEADERS = {"User-Agent": "FTSeek-US/2.0 News Pipeline"}


def _load_finnhub_key() -> str:
    key = os.getenv("FINNHUB_API_KEY", "")
    if key:
        return key
    root = Path(__file__).resolve().parents[3]
    env_path = root / ".env.us"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("FINNHUB_API_KEY="):
                return line.split("=", 1)[1].strip()
    return ""


def parse_finnhub_news(json_text: str, fetched_at: Optional[str] = None) -> List[CanonicalEvent]:
    fetched_at = fetched_at or datetime.now(timezone.utc).isoformat()
    rows = json.loads(json_text)
    events = []
    for row in rows:
        headline = row.get("headline") or ""
        url = row.get("url") or ""
        timestamp = row.get("datetime")
        event_time = None
        if timestamp is not None:
            event_time = datetime.fromtimestamp(int(timestamp), timezone.utc).isoformat()
        summary = row.get("summary") or ""
        source = row.get("source") or ""
        event_id = make_event_id(SOURCE_ID, url, headline, event_time)
        events.append(
            CanonicalEvent(
                event_id=event_id,
                source_id=SOURCE_ID,
                source_tier=SOURCE_TIER,
                source_url=url,
                event_type="NEWS_WIRE",
                event_time=event_time,
                headline=headline,
                summary=str(summary)[:500],
                facts={"source": source, "category": "general"},
                status="UNVERIFIED",
                confidence=0.50,
                reasoning_version="v0",
            )
        )
    return events


def fetch_finnhub_news(category: str = "general") -> SourceResult:
    key = _load_finnhub_key()
    if not key:
        return SourceResult(
            source_id=SOURCE_ID,
            source_tier=SOURCE_TIER,
            status="ERROR",
            latency_ms=0,
            error="FINNHUB_API_KEY missing",
        )
    url = f"{URL}?category={category}&token={key}"
    started = time.perf_counter()
    try:
        response = requests.get(url, headers=HEADERS, timeout=20)
        latency = int((time.perf_counter() - started) * 1000)
        response.raise_for_status()
        payload_hash = hashlib.sha256(response.content).hexdigest()
        raw = RawEvent(
            raw_id=make_raw_id(SOURCE_ID, payload_hash),
            source_id=SOURCE_ID,
            source_tier=SOURCE_TIER,
            source_url=f"{URL}?category={category}&token=<redacted>",
            source_published_at=None,
            fetched_at=datetime.now(timezone.utc).isoformat(),
            payload_hash=payload_hash,
            raw_artifact=response.text,
            parse_status="PARSED",
        )
        events = parse_finnhub_news(response.text)
        return SourceResult(
            source_id=SOURCE_ID,
            source_tier=SOURCE_TIER,
            status="OK",
            latency_ms=latency,
            raw_event=raw,
            events=events,
        )
    except Exception as exc:
        latency = int((time.perf_counter() - started) * 1000)
        return SourceResult(
            source_id=SOURCE_ID,
            source_tier=SOURCE_TIER,
            status="ERROR",
            latency_ms=latency,
            error=f"{type(exc).__name__}: {exc}",
        )
