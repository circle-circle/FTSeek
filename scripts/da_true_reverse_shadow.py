#!/usr/bin/env python3
"""Build an append-only true-reverse ledger from DA checkpoint artifacts."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.playbook.true_reverse_shadow import (  # noqa: E402
    append_events,
    build_true_reverse_event,
)

logger = logging.getLogger("ftseek.scripts.da_true_reverse_shadow")


def _json_documents(directory: Path) -> Iterator[tuple[Path, dict[str, Any]]]:
    for path in sorted(directory.glob("*.json")):
        if path.name.endswith(".raw.json") or path.name.endswith(".bak"):
            continue
        try:
            yield path, json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as read_error:
            logger.warning(
                "[true-reverse-shadow] checkpoint 读取失败已跳过: %s (%s)",
                path, read_error, exc_info=True,
            )


def scan_checkpoints(
    checkpoint_root: Path, since: str
) -> list[dict[str, Any]]:
    events = []
    for directory in sorted(checkpoint_root.iterdir()):
        if not directory.is_dir() or directory.name < since:
            continue
        for path, document in _json_documents(directory):
            analysis = document.get("_llm_output") or {}
            debate = analysis.get("_debate") or {}
            event = build_true_reverse_event(
                date=directory.name,
                symbol=str(document.get("symbol") or path.stem),
                analysis=analysis,
                debate=debate,
                artifact=(
                    str(path.relative_to(ROOT))
                    if path.is_relative_to(ROOT)
                    else str(path)
                ),
            )
            if event is not None:
                events.append(event)
    return events


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", default="2026-09-01")
    parser.add_argument(
        "--checkpoint-root",
        type=Path,
        default=ROOT / "data" / "da_checkpoint",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "data" / "shadow" / "da_true_reverse.jsonl",
    )
    args = parser.parse_args()
    events = scan_checkpoints(args.checkpoint_root, args.since)
    appended, skipped = append_events(events, args.output)
    independent = sum(
        1 for event in events
        if event["classification"] == "independent_true_reverse"
    )
    print(
        f"[true-reverse-shadow] scanned_since={args.since} candidates={len(events)} "
        f"independent_true_reverse={independent} appended={appended} "
        f"existing_skipped={skipped} output={args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
