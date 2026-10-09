#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.event_stream.ue_clue_adapter import gather_event_stream_clue


def main() -> int:
    parser = argparse.ArgumentParser(description="Print recent EventStream events as UE clue text.")
    parser.add_argument("--db", type=Path, default=None, help="SQLite path; default uses project data dir")
    parser.add_argument("--limit", type=int, default=20, help="Maximum events to render")
    parser.add_argument("--relevant-only", action="store_true", help="Keep watchlist/sector-relevant events only")
    parser.add_argument("--dedupe", action="store_true", help="Collapse cross-source duplicate events")
    parser.add_argument("--scan-limit", type=int, default=500, help="Events inspected before filtering")
    args = parser.parse_args()
    clue = gather_event_stream_clue(
        args.db,
        args.limit,
        relevant_only=args.relevant_only,
        dedupe=args.dedupe,
        scan_limit=args.scan_limit,
    )
    if clue:
        print(clue)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
