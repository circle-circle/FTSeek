from __future__ import annotations

import hashlib
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import List, Optional

import requests

from app.event_stream.models import (
    CanonicalEvent,
    RawEvent,
    SourceResult,
    make_event_id,
    make_raw_id,
)

SOURCE_ID = "SEC_EDGAR_8K_ATOM"
SOURCE_TIER = "T0"
URL = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&output=atom&count=40"
HEADERS = {"User-Agent": "FTSeek-Research admin@ftseek.com"}


def parse_sec_edgar_atom(xml_text: str, fetched_at: Optional[str] = None) -> List[CanonicalEvent]:
    fetched_at = fetched_at or datetime.now(timezone.utc).isoformat()
    root = ET.fromstring(xml_text)
    entries = [node for node in root.iter() if node.tag.endswith("entry")]
    events = []
    for entry in entries:
        title = next((node.text for node in entry if node.tag.endswith("title")), "") or ""
        # 2026-10-09 F2 修复: Atom 的 <link> 是 href 属性而非元素文本;
        #   旧实现只取 node.text ⇒ 生产库 52/52 行 source_url 全空, 断可溯源链。
        link = ""
        for node in entry:
            if node.tag.endswith("link"):
                link = (node.attrib.get("href") or (node.text or "")).strip()
                break
        updated = next((node.text for node in entry if node.tag.endswith("updated")), "") or ""
        summary = next((node.text for node in entry if node.tag.endswith("summary")), "") or ""
        event_id = make_event_id(SOURCE_ID, link, title, updated)
        events.append(
            CanonicalEvent(
                event_id=event_id,
                source_id=SOURCE_ID,
                source_tier=SOURCE_TIER,
                source_url=link,
                event_type="SEC_8K_FILING",
                event_time=updated,
                headline=title,
                summary=summary[:500],
                facts={"form": "8-K", "source_feed": URL},
                status="UNVERIFIED",
                confidence=0.80,
                reasoning_version="v0",
            )
        )
    return events


def fetch_sec_edgar_atom(count: int = 40) -> SourceResult:
    url = URL.replace("count=40", f"count={count}")
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
            source_url=url,
            source_published_at=None,
            fetched_at=datetime.now(timezone.utc).isoformat(),
            payload_hash=payload_hash,
            raw_artifact=response.text,
            parse_status="PARSED",
        )
        events = parse_sec_edgar_atom(response.text)
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
