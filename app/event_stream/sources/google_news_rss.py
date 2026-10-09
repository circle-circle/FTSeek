from __future__ import annotations

import hashlib
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional
from urllib.parse import quote

import requests

from app.event_stream.models import (
    CanonicalEvent,
    RawEvent,
    SourceResult,
    make_event_id,
    make_raw_id,
)

SOURCE_ID = "GOOGLE_NEWS_RSS"
SOURCE_TIER = "T2"
BASE_URL = "https://news.google.com/rss/search"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; FTSeek-US/2.0)"}

# 2026-10-09 F1 修复: 查询词不再写死样本事件 ("SpaceX FCC" 曾导致 107/107 生产行
#   单一 query, 违反 DEB-20261009 §2.3 可泛化)。默认值仅兜底, 生产口径以
#   config/unified_config.yaml 的 event_stream.google_news_queries 为准。
#   查询设计锚定相关性过滤器的消费面: 监管机关 + sector 触发词英文面。
DEFAULT_QUERIES: List[str] = [
    "FCC OR FTC OR FDA OR \"Securities and Exchange Commission\"",
    "spectrum OR direct-to-cell OR satellite broadband OR wireless carrier",
    "semiconductor OR chip export OR sanctions",
    "merger OR acquisition OR takeover",
]


def _configured_queries() -> List[str]:
    """读 event_stream.google_news_queries; 缺配置/解析失败回退 DEFAULT_QUERIES。"""
    try:
        import yaml

        config_path = (
            Path(__file__).resolve().parents[3] / "config" / "unified_config.yaml"
        )
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        queries = (
            (data.get("event_stream") or {}).get("google_news_queries")
        )
        if isinstance(queries, list) and queries:
            return [str(query) for query in queries if str(query).strip()]
    except Exception as exc:
        print(f"  [warn] google_news_queries 配置读取失败 (降级默认): {exc}")
    return list(DEFAULT_QUERIES)


def parse_google_news_rss(
    xml_text: str,
    fetched_at: Optional[str] = None,
    query: Optional[str] = None,
) -> List[CanonicalEvent]:
    fetched_at = fetched_at or datetime.now(timezone.utc).isoformat()
    root = ET.fromstring(xml_text)
    items = [node for node in root.iter() if node.tag.endswith("item")]
    events = []
    for item in items:
        title = next((node.text for node in item if node.tag.endswith("title")), "") or ""
        link = next((node.text for node in item if node.tag.endswith("link")), "") or ""
        published = next((node.text for node in item if node.tag.endswith("pubDate")), "") or ""
        summary = next((node.text for node in item if node.tag.endswith("description")), "") or ""
        source = next((node.text for node in item if node.tag.endswith("source")), "") or ""
        event_id = make_event_id(SOURCE_ID, link, title, published)
        events.append(
            CanonicalEvent(
                event_id=event_id,
                source_id=SOURCE_ID,
                source_tier=SOURCE_TIER,
                source_url=link,
                event_type="NEWS_DISCOVERY",
                event_time=published,
                headline=title,
                summary=summary[:500],
                facts={"source": source, "query": query},
                status="UNVERIFIED",
                confidence=0.30,
                reasoning_version="v0",
            )
        )
    return events


def fetch_google_news_rss(queries: Optional[List[str]] = None) -> SourceResult:
    """按 query 列表逐一抓取并合并 (event_id 去重)。

    单 query 失败不拖垮整源: 部分失败仍返回 OK 并在 error 字段留痕;
    全部失败才返回 ERROR。raw artifact 按 query 分段拼接, 哈希覆盖全量。
    """
    query_list = [str(q) for q in queries] if queries else _configured_queries()
    started = time.perf_counter()
    merged: List[CanonicalEvent] = []
    seen_ids = set()
    artifacts = []
    errors = []
    for index, query in enumerate(query_list):
        if index:
            time.sleep(0.5)  # 礼貌间隔, 避免触发限流
        url = f"{BASE_URL}?q={quote(query)}&hl=en-US&gl=US&ceid=US:en"
        try:
            response = requests.get(url, headers=HEADERS, timeout=20)
            response.raise_for_status()
            artifacts.append(f"##### query={query} url={url}\n{response.text}")
            for event in parse_google_news_rss(response.text, query=query):
                if event.event_id in seen_ids:
                    continue
                seen_ids.add(event.event_id)
                merged.append(event)
        except Exception as exc:
            errors.append(f"[{query}] {type(exc).__name__}: {exc}")
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
        source_url=f"{BASE_URL} (queries={len(query_list)})",
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
