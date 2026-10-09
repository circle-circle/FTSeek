#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.event_stream.store import EventStore

DEFAULT_SCAN_PATH = ROOT / "data" / "shadow" / "llm_universe_scan.jsonl"


def _today_et() -> str:
    from datetime import datetime

    return datetime.now(ZoneInfo("America/New_York")).date().isoformat()


def _latest_scan(path: Path, date: str) -> dict | None:
    latest: dict | None = None
    if not path.exists():
        return None
    bad_lines = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            bad_lines += 1
            continue
        if row.get("type") != "scan" or row.get("date") != date:
            continue
        if latest is None or str(row.get("scanned_at", "")) >= str(latest.get("scanned_at", "")):
            latest = row
    if bad_lines:
        # 2026-10-09 复查棘轮清剿: 坏行计数留痕, 不得静默 (R5 模式)
        print(f"  [warn] scan jsonl 坏行 {bad_lines} 条已跳过", file=sys.stderr)
    return latest


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check whether the latest UE scan consumed EventStream clues."
    )
    parser.add_argument("--scan-path", type=Path, default=DEFAULT_SCAN_PATH)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--date", default=_today_et())
    parser.add_argument("--allow-empty", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    scan = _latest_scan(args.scan_path, args.date)
    stats = ((scan or {}).get("brief_material_stats") or {})
    event_stream_count = int(stats.get("event_stream", 0) or 0)
    source_health = EventStore(args.db).get_source_health_rows()
    unhealthy = [row for row in source_health if row["status"] != "OK"]
    passed = scan is not None and (args.allow_empty or event_stream_count > 0) and not unhealthy
    payload = {
        "date": args.date,
        "scan_found": scan is not None,
        "scan_window": (scan or {}).get("scan_window"),
        "scanned_at": (scan or {}).get("scanned_at"),
        "event_stream_clue_count": event_stream_count,
        "source_health": source_health,
        "passed": passed,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(
            f"scan_found={payload['scan_found']} "
            f"scan_window={payload.get('scan_window')} "
            f"scanned_at={payload.get('scanned_at')}"
        )
        print(f"event_stream_clue_count={event_stream_count}")
        print(f"source_health_ok={not unhealthy}")
        print(f"passed={passed}")
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
