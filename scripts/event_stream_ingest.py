#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
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
    print(f"[event_stream] total_latency_ms={service.last_total_latency_ms}", file=sys.stderr)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        for source_id, info in summary.items():
            status = info.get("status", "?")
            latency = info.get("latency_ms", "?")
            events = info.get("events_inserted", 0)
            print(f"{source_id:<27} status={status} latency={latency}ms events={events}")
    # 2026-10-18: 新鲜度/不健康折叠进 ingest 日志 (原先只靠未挂 cron 的独立检查).
    try:
        now = datetime.now(timezone.utc)
        warnings = []
        for row in store.get_source_health_rows():
            source_id = str(row.get("source_id") or "?")
            status = str(row.get("status") or "?")
            if status == "PARTIAL":
                warnings.append(f"{source_id}:PARTIAL")
            elif status != "OK":
                warnings.append(f"{source_id}:{status}")
                continue
            last = row.get("last_success_at")
            if last:
                try:
                    stamp = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
                    if stamp.tzinfo is None:
                        stamp = stamp.replace(tzinfo=timezone.utc)
                    age = int((now - stamp).total_seconds())
                    if age > 1800:
                        warnings.append(f"{source_id}:stale{age}s")
                except ValueError:
                    warnings.append(f"{source_id}:bad_ts")
        if warnings:
            print(f"[event_stream] health_warning: {'; '.join(warnings)}", file=sys.stderr)
    except Exception as exc:
        print(f"[event_stream] health_warning_check_failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
