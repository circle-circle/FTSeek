#!/usr/bin/env python3
"""DEB-20261009 Phase 0 external source audit.

Read-only probe for public primary/regulatory/discovery sources. The script does
not write repository data and never prints API credentials.
"""
from __future__ import annotations

import argparse
import json
import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TIMEOUT = 20.0


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _finnhub_key() -> str:
    key = os.getenv("FINNHUB_API_KEY", "")
    if key:
        return key
    env_path = ROOT / ".env.us"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("FINNHUB_API_KEY="):
                return line.split("=", 1)[1].strip()
    return ""


def _parse_sec_atom(text: str) -> dict[str, Any]:
    root = ET.fromstring(text)
    entries = [node for node in root.iter() if node.tag.endswith("entry")]
    first = entries[0] if entries else None
    title = next((node.text for node in first if node.tag.endswith("title")), None) if first else None
    timestamp = next(
        (node.text for node in first if node.tag.endswith(("updated", "published"))),
        None,
    ) if first else None
    return {
        "result_count": len(entries),
        "first_title": title,
        "first_timestamp": timestamp,
        "timestamp_fields": ["updated/published"],
        "sample_hits": [],
    }


def _parse_sec_full_text(text: str) -> dict[str, Any]:
    data = json.loads(text)
    hits = data.get("hits", {})
    rows = hits.get("hits", [])
    total = hits.get("total", {}).get("value")
    first_source = rows[0].get("_source", {}) if rows else {}
    names = first_source.get("display_names") or []
    return {
        "result_count": total if total is not None else len(rows),
        "first_title": names[0] if names else None,
        "first_timestamp": first_source.get("file_date"),
        "timestamp_fields": ["file_date"],
        "sample_hits": [],
    }


def _parse_federal_register(text: str) -> dict[str, Any]:
    data = json.loads(text)
    rows = data.get("results", [])
    first = rows[0] if rows else {}
    return {
        "result_count": data.get("count", len(rows)),
        "first_title": first.get("title"),
        "first_timestamp": first.get("publication_date"),
        "timestamp_fields": ["publication_date"],
        "sample_hits": [],
    }


def _parse_rss(text: str) -> dict[str, Any]:
    root = ET.fromstring(text)
    items = [node for node in root.iter() if node.tag.endswith("item")]
    first = items[0] if items else None
    title = next((node.text for node in first if node.tag.endswith("title")), None) if first else None
    timestamp = next((node.text for node in first if node.tag.endswith("pubDate")), None) if first else None
    sample_hits = []
    for item in items:
        item_title = next((node.text for node in item if node.tag.endswith("title")), "") or ""
        if "spacex" in item_title.lower():
            sample_hits.append(item_title)
        if len(sample_hits) >= 3:
            break
    return {
        "result_count": len(items),
        "first_title": title,
        "first_timestamp": timestamp,
        "timestamp_fields": ["pubDate"],
        "sample_hits": sample_hits,
    }


def _parse_finnhub(text: str) -> dict[str, Any]:
    rows = json.loads(text)
    first = rows[0] if rows else {}
    first_timestamp = None
    if first.get("datetime"):
        first_timestamp = datetime.fromtimestamp(int(first["datetime"]), timezone.utc).isoformat()
    sample_hits = []
    for row in rows:
        if "spacex" in json.dumps(row).lower():
            timestamp = datetime.fromtimestamp(int(row["datetime"]), timezone.utc).isoformat()
            sample_hits.append({
                "headline": row.get("headline"),
                "source": row.get("source"),
                "published_at": timestamp,
                "url": row.get("url"),
            })
        if len(sample_hits) >= 3:
            break
    return {
        "result_count": len(rows),
        "first_title": first.get("headline"),
        "first_timestamp": first_timestamp,
        "timestamp_fields": ["datetime"],
        "sample_hits": sample_hits,
    }


def _parse_unstructured(_: str) -> dict[str, Any]:
    return {
        "result_count": 1,
        "first_title": None,
        "first_timestamp": None,
        "timestamp_fields": [],
        "sample_hits": [],
    }


PARSERS: dict[str, Callable[[str], dict[str, Any]]] = {
    "sec_atom": _parse_sec_atom,
    "sec_full_text": _parse_sec_full_text,
    "federal_register": _parse_federal_register,
    "rss": _parse_rss,
    "finnhub": _parse_finnhub,
    "html": _parse_unstructured,
    "pdf": _parse_unstructured,
}


def source_specs() -> list[dict[str, Any]]:
    sec_headers = {"User-Agent": "FTSeek-Research admin@ftseek.com"}
    news_headers = {"User-Agent": "FTSeek-US/2.0 News Pipeline"}
    browser_headers = {"User-Agent": "Mozilla/5.0 (compatible; FTSeek-US/2.0)"}
    return [
        {
            "source_id": "SEC_EDGAR_CURRENT_8K_ATOM",
            "tier": "T0",
            "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&output=atom&count=40",
            "kind": "sec_atom",
            "headers": sec_headers,
        },
        {
            "source_id": "SEC_EDGAR_FULL_TEXT",
            "tier": "T0",
            "url": "https://efts.sec.gov/LATEST/search-index?q=SpaceX&dateRange=custom&startdt=2026-10-01&enddt=2026-10-09",
            "kind": "sec_full_text",
            "headers": sec_headers,
        },
        {
            "source_id": "FCC_EDOCS_ORDER_PDF",
            "tier": "T0",
            "url": "https://docs.fcc.gov/public/attachments/DA-26-1078A1.pdf",
            "kind": "pdf",
            "headers": sec_headers,
        },
        {
            "source_id": "FEDERAL_REGISTER_API",
            "tier": "T0",
            "url": "https://www.federalregister.gov/api/v1/documents.json?conditions%5Bterm%5D=SpaceX&order=newest&per_page=10",
            "kind": "federal_register",
            "headers": sec_headers,
        },
        {
            "source_id": "SPACEX_IR",
            "tier": "T0",
            "url": "https://www.spacex.com/updates/",
            "kind": "html",
            "headers": browser_headers,
        },
        {
            "source_id": "GRAIN_IR",
            "tier": "T0",
            "url": "https://www.grainmanagement.com/",
            "kind": "html",
            "headers": browser_headers,
        },
        {
            "source_id": "GOOGLE_NEWS_RSS",
            "tier": "T2",
            "url": "https://news.google.com/rss/search?q=SpaceX%20FCC&hl=en-US&gl=US&ceid=US:en",
            "kind": "rss",
            "headers": browser_headers,
        },
        {
            "source_id": "RSSHUB_ROOT",
            "tier": "T2",
            "url": "https://rsshub.rssforever.com/",
            "kind": "html",
            "headers": news_headers,
        },
        {
            "source_id": "RSSHUB_BLOOMBERG_ROUTE",
            "tier": "T2",
            "url": "https://rsshub.rssforever.com/bloomberg/markets",
            "kind": "rss",
            "headers": news_headers,
        },
    ]


def audit_sources() -> dict[str, Any]:
    specs = source_specs()
    key = _finnhub_key()
    if key:
        specs.append({
            "source_id": "FINNHUB_GENERAL_NEWS",
            "tier": "T1",
            "url": f"https://finnhub.io/api/v1/news?category=general&token={key}",
            "kind": "finnhub",
            "headers": {"User-Agent": "FTSeek-US/2.0 News Pipeline"},
        })
    results = []
    for spec in specs:
        started = time.perf_counter()
        row: dict[str, Any] = {
            "source_id": spec["source_id"],
            "tier": spec["tier"],
            "url": spec["url"] if "token=" not in spec["url"] else spec["url"].split("token=")[0] + "token=<redacted>",
        }
        try:
            response = requests.get(spec["url"], headers=spec["headers"], timeout=DEFAULT_TIMEOUT)
            elapsed = round((time.perf_counter() - started) * 1000)
            row.update({
                "http_status": response.status_code,
                "latency_ms": elapsed,
                "content_type": response.headers.get("content-type", ""),
                "bytes": len(response.content),
            })
            if 200 <= response.status_code < 300:
                row.update(PARSERS[spec["kind"]](response.text))
            else:
                row.update({
                    "result_count": 0,
                    "first_title": None,
                    "first_timestamp": None,
                    "timestamp_fields": [],
                    "sample_hits": [],
                })
        except Exception as exc:
            elapsed = round((time.perf_counter() - started) * 1000)
            row.update({
                "http_status": None,
                "latency_ms": elapsed,
                "error": f"{type(exc).__name__}: {exc}"[:240],
            })
        results.append(row)
    return {"generated_at": _utc_now(), "results": results}


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit Phase 0 event sources (read-only).")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    args = parser.parse_args()
    report = audit_sources()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"Phase 0 source audit generated_at={report['generated_at']}")
        for row in report["results"]:
            status = row.get("http_status", "ERR")
            print(
                f"{row['source_id']:<27} tier={row['tier']} status={status} "
                f"latency={row.get('latency_ms')}ms items={row.get('result_count')} "
                f"timestamp={row.get('first_timestamp') or '-'}"
            )
            for hit in row.get("sample_hits", []):
                print(f"  sample: {hit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
