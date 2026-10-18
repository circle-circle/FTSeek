from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


def make_raw_id(source_id: str, payload_hash: str) -> str:
    raw = f"{source_id}:{payload_hash}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:20]


def make_event_id(
    source_id: str,
    source_url: str,
    headline: str,
    event_time: Optional[str],
) -> str:
    raw = f"{source_id}|{source_url}|{headline}|{event_time or ''}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:20]


@dataclass
class RawEvent:
    raw_id: str
    source_id: str
    source_tier: str
    source_url: str
    source_published_at: Optional[str]
    fetched_at: str
    payload_hash: str
    raw_artifact: str
    parse_status: str = "PENDING"
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CanonicalEvent:
    event_id: str
    source_id: str
    source_tier: str
    source_url: str
    event_type: str
    event_time: Optional[str]
    headline: str
    summary: str
    facts: Dict[str, Any] = field(default_factory=dict)
    status: str = "UNVERIFIED"
    confidence: float = 0.0
    reasoning_version: str = "v0"
    event_meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SourceResult:
    source_id: str
    source_tier: str
    status: str
    latency_ms: int
    raw_event: Optional[RawEvent] = None
    events: List[CanonicalEvent] = field(default_factory=list)
    error: Optional[str] = None
