#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SEC 源影子对比 (只读, 2026-10-18).

目的: 在把 EventStream 的 raw SEC_EDGAR_8K_ATOM 换成 SEC_FILINGS_PROVIDER 之前,
量化两者覆盖/重复/附加字段, 避免盲目切源或再建第四条 SEC 路径.

对比:
  - raw: fetch_sec_edgar_atom (8-K Atom)
  - provider: fetch_sec_filings_provider (8-K + 13D/G + Form4)

只读: 不写 canonical_events/playbook; 仅打印/输出对比结果.
用法:
  PYTHONPATH=. ./venv/bin/python scripts/event_stream_sec_compare.py
  ... --json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.event_stream.event_schema import build_event_meta
from app.event_stream.sources.sec_edgar_atom import fetch_sec_edgar_atom
from app.event_stream.sources.sec_provider import fetch_sec_filings_provider


def _norm_url(url: str) -> str:
    try:
        parsed = urlsplit(str(url or ""))
        return f"{parsed.netloc.lower().removeprefix('www.')}{parsed.path.rstrip('/')}"
    except Exception:
        return str(url or "").strip().lower()


def _event_ticker(event) -> str:
    facts = event.facts if isinstance(event.facts, dict) else {}
    return str(facts.get("ticker") or "").strip().upper().removeprefix("US.")


def compare_results(raw_events: Iterable[Any], provider_events: Iterable[Any]) -> Dict[str, Any]:
    raw = list(raw_events)
    provider = list(provider_events)
    raw_urls = {_norm_url(e.source_url) for e in raw if e.source_url}
    provider_urls = {_norm_url(e.source_url) for e in provider if e.source_url}
    overlap = raw_urls & provider_urls

    def _types(events: List[Any]) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for event in events:
            out[event.event_type] = out.get(event.event_type, 0) + 1
        return dict(sorted(out.items()))

    provider_8k = [e for e in provider if e.event_type == "SEC_8K_FILING"]
    provider_8k_with_items = [
        e for e in provider_8k
        if isinstance(e.facts, dict) and e.facts.get("items")
    ]
    raw_tickers = {_event_ticker(e) for e in raw if _event_ticker(e)}
    provider_tickers = {_event_ticker(e) for e in provider if _event_ticker(e)}
    return {
        "raw_n": len(raw),
        "provider_n": len(provider),
        "raw_by_type": _types(raw),
        "provider_by_type": _types(provider),
        "url_overlap": len(overlap),
        "raw_only": len(raw_urls - provider_urls),
        "provider_only": len(provider_urls - raw_urls),
        "raw_tickers": sorted(raw_tickers),
        "provider_tickers": sorted(provider_tickers),
        "ticker_overlap": len(raw_tickers & provider_tickers),
        "provider_8k_n": len(provider_8k),
        "provider_8k_with_items": len(provider_8k_with_items),
        "provider_8k_item_coverage_pct": (
            round(100.0 * len(provider_8k_with_items) / len(provider_8k), 1)
            if provider_8k else None
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="SEC raw Atom vs provider shadow compare")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    raw = fetch_sec_edgar_atom()
    provider = fetch_sec_filings_provider()
    result = compare_results(raw.events, provider.events)
    result["raw_status"] = raw.status
    result["provider_status"] = provider.status
    result["raw_latency_ms"] = raw.latency_ms
    result["provider_latency_ms"] = provider.latency_ms
    result["raw_error"] = raw.error
    result["provider_error"] = provider.error

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"raw={result['raw_n']} ({result['raw_status']}, {result['raw_latency_ms']}ms)")
        print(f"provider={result['provider_n']} ({result['provider_status']}, {result['provider_latency_ms']}ms)")
        print(f"url_overlap={result['url_overlap']} raw_only={result['raw_only']} provider_only={result['provider_only']}")
        print(f"tickers raw={len(result['raw_tickers'])} provider={len(result['provider_tickers'])} overlap={result['ticker_overlap']}")
        print(f"provider 8-K item coverage={result['provider_8k_item_coverage_pct']}%")
        print(f"raw_by_type={result['raw_by_type']}")
        print(f"provider_by_type={result['provider_by_type']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
