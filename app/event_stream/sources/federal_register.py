from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests

from app.event_stream.models import (
    CanonicalEvent,
    RawEvent,
    SourceResult,
    make_event_id,
    make_raw_id,
)

SOURCE_ID = "FEDERAL_REGISTER_API"
SOURCE_TIER = "T0"
BASE_URL = "https://www.federalregister.gov/api/v1/documents.json"
HEADERS = {"User-Agent": "FTSeek-Research admin@ftseek.com"}

# 2026-10-09 F1 修复: term 不再写死样本事件 ("SpaceX")。默认值仅兜底, 生产口径以
#   config/unified_config.yaml 的 event_stream.federal_register_terms 为准。
#   FR 是监管通知源, 默认按监管机关维度取最新。
DEFAULT_TERMS: List[str] = [
    "Federal Communications Commission",
    "Securities and Exchange Commission",
]


def _configured_terms() -> List[str]:
    """读 event_stream.federal_register_terms; 缺配置/解析失败回退 DEFAULT_TERMS。"""
    try:
        import yaml

        config_path = (
            Path(__file__).resolve().parents[3] / "config" / "unified_config.yaml"
        )
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        terms = (data.get("event_stream") or {}).get("federal_register_terms")
        if isinstance(terms, list) and terms:
            return [str(term) for term in terms if str(term).strip()]
    except Exception as exc:
        print(f"  [warn] federal_register_terms 配置读取失败 (降级默认): {exc}")
    return list(DEFAULT_TERMS)


def parse_federal_register(
    json_text: str,
    fetched_at: Optional[str] = None,
    term: Optional[str] = None,
) -> List[CanonicalEvent]:
    fetched_at = fetched_at or datetime.now(timezone.utc).isoformat()
    data: Dict[str, Any] = json.loads(json_text)
    rows = data.get("results", [])
    events = []
    for row in rows:
        title = row.get("title") or ""
        url = row.get("html_url") or row.get("url") or ""
        event_time = row.get("publication_date")
        summary = row.get("abstract") or row.get("explanation") or ""
        event_id = make_event_id(SOURCE_ID, url, title, event_time)
        events.append(
            CanonicalEvent(
                event_id=event_id,
                source_id=SOURCE_ID,
                source_tier=SOURCE_TIER,
                source_url=url,
                event_type="FEDERAL_REGISTER_NOTICE",
                event_time=event_time,
                headline=title,
                summary=str(summary)[:500],
                facts={
                    "document_number": row.get("document_number"),
                    "type": row.get("type"),
                    "agencies": row.get("agencies", []),
                    "term": term,
                },
                status="UNVERIFIED",
                confidence=0.80,
                reasoning_version="v0",
            )
        )
    return events


def fetch_federal_register(
    terms: Optional[List[str]] = None,
    per_page: int = 10,
) -> SourceResult:
    """按 term 列表逐一抓取并合并 (event_id 去重)。

    单 term 失败不拖垮整源: 部分失败仍返回 OK 并在 error 字段留痕;
    全部失败才返回 ERROR。raw artifact 按 term 分段拼接, 哈希覆盖全量。
    """
    term_list = [str(t) for t in terms] if terms else _configured_terms()
    started = time.perf_counter()
    merged: List[CanonicalEvent] = []
    seen_ids = set()
    artifacts = []
    errors = []
    for index, term in enumerate(term_list):
        if index:
            time.sleep(0.5)  # 礼貌间隔
        params = f"conditions%5Bterm%5D={quote(term)}&order=newest&per_page={per_page}"
        url = f"{BASE_URL}?{params}"
        try:
            response = requests.get(url, headers=HEADERS, timeout=20)
            response.raise_for_status()
            artifacts.append(f"##### term={term} url={url}\n{response.text}")
            for event in parse_federal_register(response.text, term=term):
                if event.event_id in seen_ids:
                    continue
                seen_ids.add(event.event_id)
                merged.append(event)
        except Exception as exc:
            errors.append(f"[{term}] {type(exc).__name__}: {exc}")
    latency = int((time.perf_counter() - started) * 1000)
    if not artifacts and errors:
        return SourceResult(
            source_id=SOURCE_ID,
            source_tier=SOURCE_TIER,
            status="ERROR",
            latency_ms=latency,
            error="; ".join(errors)[:500],
        )
    payload_text = "\n\n".join(artifacts)
    payload_hash = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
    raw = RawEvent(
        raw_id=make_raw_id(SOURCE_ID, payload_hash),
        source_id=SOURCE_ID,
        source_tier=SOURCE_TIER,
        source_url=f"{BASE_URL} (terms={len(term_list)})",
        source_published_at=None,
        fetched_at=datetime.now(timezone.utc).isoformat(),
        payload_hash=payload_hash,
        raw_artifact=payload_text,
        parse_status="PARSED",
    )
    return SourceResult(
        source_id=SOURCE_ID,
        source_tier=SOURCE_TIER,
        status="OK",
        latency_ms=latency,
        raw_event=raw,
        events=merged,
        error="; ".join(errors)[:500] if errors else None,
    )
