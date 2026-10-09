#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.event_stream.store import EventStore


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Report EventStore counts, source health, and first-seen latency."
    )
    parser.add_argument("--db", type=Path, default=None, help="SQLite path; default uses project data dir")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    args = parser.parse_args()

    store = EventStore(args.db)
    payload = {
        "db_path": str(store.path),
        "counts": store.get_counts(),
        "source_health": store.get_source_health_rows(),
        "latency_stats": store.get_latency_stats(),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"db={store.path}")
    counts = payload["counts"]
    print(
        "counts "
        f"raw_events={counts['raw_events']} "
        f"canonical_events={counts['canonical_events']} "
        f"source_health={counts['source_health']}"
    )
    print("source_health:")
    for row in payload["source_health"]:
        print(
            f"  {row['source_id']:<27} status={row['status']} "
            f"latency={row['latency_ms']}ms last_success={row['last_success_at']} "
            f"error={row['last_error']}"
        )
    print("published_to_seen_ms:")
    for row in payload["latency_stats"]:
        print(
            f"  {row['source_id']:<27} n={row['events_with_latency']} "
            f"avg={row['avg_published_to_seen_ms']} "
            f"min={row['min_published_to_seen_ms']} "
            f"max={row['max_published_to_seen_ms']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
