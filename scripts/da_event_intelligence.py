#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.event_stream.intelligence import (
    build_event_snapshot,
    iso_utc,
    parse_timestamp,
    read_event_records,
    read_source_health,
    run_event_replay,
)
from app.event_stream.source_registry import default_source_registry
from app.event_stream.market_brief import (
    build_brief_context,
    generate_market_event_brief,
    validate_event_snapshot,
)

DEFAULT_DB = ROOT / "data" / "event_stream.db"


def _write_json_new(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def _prepare_output(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=False)
    return path


def _expected_sources() -> List[str]:
    return sorted(default_source_registry())


def _load(db: Path, since: str, until: str):
    records = read_event_records(db, since=since, until=until)
    health = read_source_health(db)
    return records, health, _expected_sources()


def _write_replay(output_dir: Path, replay: Dict[str, Any]) -> None:
    snapshot_files = []
    for index, snapshot in enumerate(replay["snapshots"]):
        filename = f"snapshots/{index + 1:04d}_{snapshot['as_of'].replace(':', '').replace('+', 'p')}.json"
        _write_json_new(output_dir / filename, snapshot)
        snapshot_files.append(filename)
    manifest = {key: value for key, value in replay.items() if key != "snapshots"}
    manifest["snapshot_files"] = snapshot_files
    _write_json_new(output_dir / "replay_manifest.json", manifest)


def _headlines(snapshot: Dict[str, Any]) -> List[str]:
    return [
        str(event.get("headline") or "")
        for cluster in snapshot["clusters"]
        for event in cluster["events"]
    ]


def _contains(snapshot: Dict[str, Any], fragment: str) -> bool:
    return any(fragment in headline for headline in _headlines(snapshot))


def _smoke(db: Path, output_dir: Path, lookback_hours: int) -> Dict[str, Any]:
    checkpoints = [
        "2026-11-05T13:35:00+00:00",
        "2026-11-05T15:05:00+00:00",
        "2026-11-05T18:50:00+00:00",
    ]
    since = iso_utc(parse_timestamp(checkpoints[0]) - timedelta(hours=lookback_hours))
    records, health, expected = _load(db, since, checkpoints[-1])
    replay = run_event_replay(
        records,
        checkpoints=checkpoints,
        since=since,
        source_health=health,
        expected_sources=expected,
    )
    _prepare_output(output_dir)
    _write_replay(output_dir, replay)
    first, second, third = replay["snapshots"]
    checks = {
        "premarket_has_blue_wave": _contains(first, "blue wave will drown data centres"),
        "premarket_has_ai_split": _contains(first, "sharp split in the AI trade"),
        "premarket_excludes_ai_stocks_late_arrival": not _contains(first, "AI stocks fall, oil bounces back"),
        "premarket_excludes_chip_late_arrival": not _contains(first, "Chip selloff drags global stocks lower"),
        "second_checkpoint_has_ai_stocks": _contains(second, "AI stocks fall, oil bounces back"),
        "second_checkpoint_excludes_chip": not _contains(second, "Chip selloff drags global stocks lower"),
        "third_checkpoint_has_chip": _contains(third, "Chip selloff drags global stocks lower"),
        "all_snapshots_grounded": all(not validate_event_snapshot(item) for item in replay["snapshots"]),
        "zero_llm_calls": replay["llm_calls"] == 0,
        "latency_has_unit": first["latency"]["ALL"]["unit"] == "milliseconds",
    }
    summary = {
        "schema": "event_intelligence_r0_smoke_v1",
        "passed": all(checks.values()),
        "checks": checks,
        "snapshot_ids": [item["snapshot_id"] for item in replay["snapshots"]],
        "premarket_event_count": first["event_count"],
        "premarket_latency": first["latency"]["ALL"],
        "output_dir": str(output_dir),
        "db_mode": "read-only",
        "production_writes": False,
    }
    _write_json_new(output_dir / "smoke_summary.json", summary)
    return summary


def _dashscope_call(system: str, user: str, **kwargs) -> Dict[str, Any]:
    skill_scripts = Path("/root/.hermes/skills/deep-stock-analysis/scripts")
    if str(skill_scripts) not in sys.path:
        sys.path.insert(0, str(skill_scripts))
    from llm_synth import _call_llm
    return _call_llm(system, user, temperature=0.1, **kwargs)


def _brief(args: argparse.Namespace) -> int:
    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    snapshot_errors = validate_event_snapshot(snapshot)
    if snapshot_errors:
        raise ValueError(f"snapshot grounding failed: {snapshot_errors}")
    previous_brief = json.loads(args.previous_brief.read_text(encoding="utf-8")) if args.previous_brief else None
    if previous_brief and previous_brief.get("validation_errors"):
        raise ValueError(f"previous brief validation failed: {previous_brief['validation_errors']}")
    context = build_brief_context(snapshot, limit=args.max_events, previous_brief=previous_brief)
    brief, artifacts = generate_market_event_brief(
        context,
        llm_call=_dashscope_call if args.execute else None,
        main_model=args.model,
        challenger_model=args.challenger_model,
        max_tokens=args.max_tokens,
        timeout=args.timeout,
    )
    _prepare_output(args.output_dir)
    _write_json_new(args.output_dir / "brief_context.json", context)
    _write_json_new(args.output_dir / "market_event_brief.json", brief)
    for key, filename in (
        ("main_system", "main_system.txt"), ("main_user", "main_user.json"),
        ("challenger_system", "challenger_system.txt"),
        ("challenger_user", "challenger_user.json"),
        ("main_raw", "main_raw.json"), ("challenger_raw", "challenger_raw.json"),
    ):
        value = artifacts.get(key)
        if value is None:
            continue
        if filename.endswith(".txt"):
            with (args.output_dir / filename).open("x", encoding="utf-8") as handle:
                handle.write(str(value) + "\n")
        else:
            _write_json_new(args.output_dir / filename, value)
    print(json.dumps({
        "brief_id": brief["brief_id"],
        "selected_fact_count": len(brief["facts"]),
        "transmission_count": len(brief["transmissions"]),
        "opposition": brief["opposition"],
        "degraded": brief["degraded"],
        "validation_errors": brief["validation_errors"],
        "llm_calls": brief["llm_calls"],
        "model_calls": brief["model_calls"],
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if not brief["validation_errors"] or brief["validation_errors"] == ["llm_not_executed"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Offline as-of Event Intelligence R0 replay")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("snapshot", "replay", "smoke", "brief"):
        subparser = subparsers.add_parser(name)
        subparser.add_argument("--db", type=Path, default=DEFAULT_DB)
        subparser.add_argument("--output-dir", type=Path, required=True)
        subparser.add_argument("--lookback-hours", type=int, default=36)
        if name == "snapshot":
            subparser.add_argument("--as-of", required=True)
        elif name == "replay":
            subparser.add_argument("--checkpoints", nargs="+", required=True)
            subparser.add_argument("--overlap-seconds", type=int, default=300)
        elif name == "brief":
            subparser.add_argument("--snapshot", type=Path, required=True)
            subparser.add_argument("--previous-brief", type=Path)
            subparser.add_argument("--execute", action="store_true")
            subparser.add_argument("--max-events", type=int, default=48)
            subparser.add_argument("--model", default="qwen3.7-max")
            subparser.add_argument("--challenger-model", default="deepseek-v4.1-flash")
            subparser.add_argument("--max-tokens", type=int, default=3000)
            subparser.add_argument("--timeout", type=int, default=120)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.lookback_hours <= 0:
        raise ValueError("lookback-hours must be positive")
    if args.command == "smoke":
        summary = _smoke(args.db, args.output_dir, args.lookback_hours)
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if summary["passed"] else 1
    if args.command == "brief":
        return _brief(args)
    if args.command == "snapshot":
        as_of = iso_utc(parse_timestamp(args.as_of, require_timezone=True))
        since = iso_utc(parse_timestamp(as_of) - timedelta(hours=args.lookback_hours))
        records, health, expected = _load(args.db, since, as_of)
        snapshot = build_event_snapshot(
            records, as_of=as_of, since=since,
            source_health=health, expected_sources=expected,
        )
        _prepare_output(args.output_dir)
        _write_json_new(args.output_dir / "snapshot.json", snapshot)
        print(json.dumps({
            "snapshot_id": snapshot["snapshot_id"],
            "event_count": snapshot["event_count"],
            "cluster_count": snapshot["cluster_count"],
            "degraded": snapshot["degraded"],
            "llm_calls": snapshot["llm_calls"],
        }, ensure_ascii=False, sort_keys=True))
        return 0
    checkpoints = [iso_utc(parse_timestamp(item, require_timezone=True)) for item in args.checkpoints]
    since = iso_utc(parse_timestamp(checkpoints[0]) - timedelta(hours=args.lookback_hours))
    records, health, expected = _load(args.db, since, checkpoints[-1])
    replay = run_event_replay(
        records, checkpoints=checkpoints, since=since,
        source_health=health, expected_sources=expected,
        overlap_seconds=args.overlap_seconds,
    )
    _prepare_output(args.output_dir)
    _write_replay(args.output_dir, replay)
    print(json.dumps({
        "snapshot_count": len(replay["snapshots"]),
        "step_statuses": [step["status"] for step in replay["steps"]],
        "llm_calls": replay["llm_calls"],
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
