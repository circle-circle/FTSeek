#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.event_stream.ingest_service import IngestService
from app.event_stream.store import EventStore


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest Phase 1 event sources into EventStore.")
    parser.add_argument("--db", type=Path, default=None, help="SQLite path; default uses project data dir")
    parser.add_argument("--source", action="append", choices=[
        "SEC_EDGAR_8K_ATOM",
        "FEDERAL_REGISTER_API",
        "GOOGLE_NEWS_RSS",
        "FINNHUB_GENERAL_NEWS",
    ], help="Source to ingest; repeatable; default all")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    args = parser.parse_args()

    store = EventStore(args.db)
    service = IngestService(store)
    summary = service.ingest(args.source)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        for source_id, info in summary.items():
            status = info.get("status", "?")
            latency = info.get("latency_ms", "?")
            events = info.get("events_inserted", 0)
            print(f"{source_id:<27} status={status} latency={latency}ms events={events}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
