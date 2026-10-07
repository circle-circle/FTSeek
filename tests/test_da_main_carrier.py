"""DA main dual-season carrier schedule contract tests."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "ops/hermes/da_main_carrier.py"
SPEC = importlib.util.spec_from_file_location("da_main_carrier", SCRIPT)
CARRIER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CARRIER)


def test_outside_window_is_silent_and_audited(monkeypatch, tmp_path, capsys):
    event_path = tmp_path / "da_main_schedule.jsonl"
    monkeypatch.setattr(CARRIER, "EVENT_PATH", event_path)
    monkeypatch.setattr(
        CARRIER, "now_et",
        lambda: datetime(2026, 10, 6, 10, 30, tzinfo=ZoneInfo("America/New_York")))
    monkeypatch.setattr(CARRIER, "is_trading_day", lambda day: True)

    assert CARRIER.main() == 0
    assert capsys.readouterr().out == ""
    event = json.loads(event_path.read_text(encoding="utf-8"))
    assert event["type"] == "da_main_schedule"
    assert event["status"] == "skipped_outside_window"
    assert event["et_minute"] == 1030


def test_window_run_passes_watchlist_symbols_to_pipeline(monkeypatch, tmp_path, capsys):
    event_path = tmp_path / "da_main_schedule.jsonl"
    monkeypatch.setattr(CARRIER, "EVENT_PATH", event_path)
    monkeypatch.setattr(CARRIER, "SCRATCH", tmp_path)
    monkeypatch.setattr(
        CARRIER, "now_et",
        lambda: datetime(2026, 10, 8, 8, 0, tzinfo=ZoneInfo("America/New_York")))
    monkeypatch.setattr(CARRIER, "is_trading_day", lambda day: True)
    monkeypatch.setattr(CARRIER, "watchlist_symbols", lambda: ["US.AAA", "US.BBB"])
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        stdout = (
            json.dumps({
                "stages": {
                    "signal": {"candidates_total": 2},
                    "output": {"analyses": [{"direction": "BUY"}]},
                }
            })
            if "--mode" in args else ""
        )
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(CARRIER.subprocess, "run", fake_run)

    assert CARRIER.main() == 0
    pipeline_args = next(args for args in calls if "--mode" in args)
    assert pipeline_args[pipeline_args.index("--symbols") + 1] == "US.AAA,US.BBB"
    assert "[DA-main]" in capsys.readouterr().out
    event = json.loads(event_path.read_text(encoding="utf-8"))
    assert event["status"] == "complete"
    assert event["candidates_total"] == 2
