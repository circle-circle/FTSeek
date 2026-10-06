"""DA main EST carrier schedule contract tests."""

from __future__ import annotations

import importlib.util
import json
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
