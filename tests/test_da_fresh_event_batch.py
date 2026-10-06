"""UE premarket → DA fresh-event candidate-batch contract tests."""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/da_fresh_event_batch.py"
SPEC = importlib.util.spec_from_file_location("da_fresh_event_batch", SCRIPT)
MOD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MOD)


def test_select_fresh_candidates_filters_skip_and_caps_count():
    scan = {
        "batch_id": "UE:fresh",
        "surfaced": [
            {"symbol": "TENB", "direction": "SHORT", "confidence": 0.45},
            {"symbol": "BLBD", "direction": "LONG", "confidence": 0.50},
            {"symbol": "WDC", "direction": "SKIP", "confidence": 0.25},
            {"symbol": "STX", "direction": "SHORT", "confidence": 0.40},
            {"symbol": "CEG", "direction": "LONG", "confidence": 0.35},
        ],
    }
    rows = MOD.select_fresh_candidates(scan, max_candidates=3)
    assert [row["symbol"] for row in rows] == ["US.TENB", "US.BLBD", "US.STX"]
    assert [row["direction"] for row in rows] == ["SHORT", "LONG", "SHORT"]


def test_load_fresh_scan_requires_premarket_and_freshness(tmp_path):
    now = datetime.now(timezone(timedelta(hours=-4)))
    stamp = (now - timedelta(minutes=5)).isoformat()
    path = tmp_path / "scan.jsonl"
    path.write_text(json.dumps({
        "type": "scan",
        "date": now.strftime("%Y-%m-%d"),
        "scan_window": "premarket",
        "batch_id": "UE:fresh",
        "batch_status": "complete",
        "started_at": stamp,
        "scanned_at": stamp,
        "surfaced": [],
    }) + "\n", encoding="utf-8")
    scan, reason = MOD.load_fresh_scan(path, now=now, max_age_hours=1)
    assert scan is not None and reason == "complete"

    stale = (now - timedelta(hours=2)).isoformat()
    path.write_text(json.dumps({
        "type": "scan", "date": now.strftime("%Y-%m-%d"),
        "scan_window": "da_feed", "batch_status": "complete",
        "started_at": stale, "scanned_at": stale,
    }) + "\n", encoding="utf-8")
    scan, reason = MOD.load_fresh_scan(path, now=now, max_age_hours=1)
    assert scan is None and reason == "no_premarket_attempt"


def test_shadow_command_is_isolated_and_execute_is_production(tmp_path):
    shadow = MOD.build_command(
        "/venv/bin/python", Path("/skill/pipeline.py"), False,
        tmp_path / "shadow", tmp_path / "context")
    assert "--fresh-event" in shadow
    assert "--no-db-write" in shadow
    assert "--no-pool-write" in shadow
    assert shadow[shadow.index("--output-dir") + 1] == str(tmp_path / "shadow")

    execute = MOD.build_command(
        "/venv/bin/python", Path("/skill/pipeline.py"), True,
        tmp_path / "shadow", tmp_path / "context")
    assert "--no-db-write" not in execute
    assert "--no-pool-write" not in execute
