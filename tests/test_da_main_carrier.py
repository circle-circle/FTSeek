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


def _events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _last(path: Path) -> dict:
    return _events(path)[-1]


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(CARRIER, "EVENT_PATH", tmp_path / "da_main_schedule.jsonl")
    monkeypatch.setattr(CARRIER, "SCRATCH", tmp_path)
    monkeypatch.setattr(CARRIER, "BRIDGE_DIR", tmp_path)
    monkeypatch.setattr(CARRIER, "DB_PATH", tmp_path / "missing.db")
    monkeypatch.setattr(CARRIER, "PIPELINE_LOG", tmp_path / "da_pipeline_stderr.log")
    monkeypatch.setattr(CARRIER, "PULLBACK_LOG", tmp_path / "pullback_shadow.log")
    monkeypatch.setattr(CARRIER, "FINALIZE_LOG", tmp_path / "da_finalize.log")
    monkeypatch.setattr(CARRIER, "LOCK_PATH", tmp_path / ".da_main_carrier.lock")


def _bridge(tmp_path, date: str = "2026-10-08", candidates: int = 1):
    (tmp_path / f"opportunity_scan_{date}.json").write_text(json.dumps({
        "trade_date": date,
        "candidates": [{"symbol": "US.AAA"}] * candidates,
    }), encoding="utf-8")


def _pipeline_stdout(directions: str = "BUY") -> str:
    return json.dumps({
        "stages": {
            "signal": {"candidates_total": 2},
            "output": {"analyses": [{"direction": directions}]},
        }
    })


def test_outside_window_is_silent_and_audited(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(
        CARRIER, "now_et",
        lambda: datetime(2026, 10, 6, 10, 30, tzinfo=ZoneInfo("America/New_York")))
    monkeypatch.setattr(CARRIER, "is_trading_day", lambda day: True)

    assert CARRIER.main() == 0
    assert capsys.readouterr().out == ""
    event = _last(CARRIER.EVENT_PATH)
    assert event["type"] == "da_main_schedule"
    assert event["status"] == "skipped_outside_window"
    assert event["et_minute"] == 1030


def test_window_run_passes_watchlist_symbols_to_pipeline(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path)
    _bridge(tmp_path)
    monkeypatch.setattr(
        CARRIER, "now_et",
        lambda: datetime(2026, 10, 8, 8, 0, tzinfo=ZoneInfo("America/New_York")))
    monkeypatch.setattr(CARRIER, "is_trading_day", lambda day: True)
    monkeypatch.setattr(CARRIER, "watchlist_symbols", lambda: ["US.AAA", "US.BBB"])
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        stdout = _pipeline_stdout() if "--mode" in args else ""
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(CARRIER.subprocess, "run", fake_run)

    assert CARRIER.main() == 0
    pipeline_args = next(args for args in calls if "--mode" in args)
    assert pipeline_args[pipeline_args.index("--symbols") + 1] == "US.AAA,US.BBB"
    assert "[DA-main]" in capsys.readouterr().out
    events = _events(CARRIER.EVENT_PATH)
    assert events[0]["status"] == "running"
    event = events[-1]
    assert event["status"] == "complete"
    assert event["candidates_total"] == 2
    assert event["artifacts"]["bridge_current"] is True


def test_empty_watchlist_continues_scanner_only(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _bridge(tmp_path)
    monkeypatch.setattr(
        CARRIER, "now_et",
        lambda: datetime(2026, 10, 8, 8, 0, tzinfo=ZoneInfo("America/New_York")))
    monkeypatch.setattr(CARRIER, "is_trading_day", lambda day: True)
    monkeypatch.setattr(CARRIER, "watchlist_symbols", lambda: [])
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        stdout = _pipeline_stdout("SKIP") if "--mode" in args else ""
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(CARRIER.subprocess, "run", fake_run)

    assert CARRIER.main() == 0
    pipeline_args = next(args for args in calls if "--mode" in args)
    assert pipeline_args[pipeline_args.index("--symbols") + 1] == ""
    statuses = [e["status"] for e in _events(CARRIER.EVENT_PATH)]
    assert "watchlist_empty_continue" in statuses


def test_pullback_timeout_is_non_blocking(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _bridge(tmp_path)
    monkeypatch.setattr(
        CARRIER, "now_et",
        lambda: datetime(2026, 10, 8, 8, 0, tzinfo=ZoneInfo("America/New_York")))
    monkeypatch.setattr(CARRIER, "is_trading_day", lambda day: True)
    monkeypatch.setattr(CARRIER, "watchlist_symbols", lambda: ["US.AAA"])
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if any("pullback_stats.py" in str(a) for a in args):
            raise subprocess.TimeoutExpired(args, 300)
        return subprocess.CompletedProcess(args, 0, stdout=_pipeline_stdout(), stderr="")

    monkeypatch.setattr(CARRIER.subprocess, "run", fake_run)

    assert CARRIER.main() == 0
    assert _last(CARRIER.EVENT_PATH)["status"] == "complete"


def test_unparsed_stdout_with_current_bridge_is_success(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _bridge(tmp_path)
    monkeypatch.setattr(
        CARRIER, "now_et",
        lambda: datetime(2026, 10, 8, 8, 0, tzinfo=ZoneInfo("America/New_York")))
    monkeypatch.setattr(CARRIER, "is_trading_day", lambda day: True)
    monkeypatch.setattr(CARRIER, "watchlist_symbols", lambda: ["US.AAA"])

    def fake_run(args, **kwargs):
        stdout = "noise before json {not-json} tail" if "--mode" in args else ""
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(CARRIER.subprocess, "run", fake_run)

    assert CARRIER.main() == 0
    event = _last(CARRIER.EVENT_PATH)
    assert event["status"] == "complete_unparsed_stdout"
    assert event["artifacts"]["bridge_current"] is True


def test_pipeline_timeout_finalizes_from_checkpoints(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(
        CARRIER, "now_et",
        lambda: datetime(2026, 10, 8, 8, 0, tzinfo=ZoneInfo("America/New_York")))
    monkeypatch.setattr(CARRIER, "is_trading_day", lambda day: True)
    monkeypatch.setattr(CARRIER, "watchlist_symbols", lambda: ["US.AAA"])

    def fake_run(args, **kwargs):
        if any("pullback_stats.py" in str(a) for a in args):
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        raise subprocess.TimeoutExpired(args, 3000)

    def fake_finalize(date):
        _bridge(tmp_path, date)
        return {"returncode": 0, "result": {"finalized": True}}

    monkeypatch.setattr(CARRIER.subprocess, "run", fake_run)
    monkeypatch.setattr(CARRIER, "_finalize", fake_finalize)

    assert CARRIER.main() == 0
    event = _last(CARRIER.EVENT_PATH)
    assert event["status"] == "pipeline_timeout_finalized"
    assert event["artifacts"]["bridge_current"] is True


def test_timeout_returncode_124_triggers_finalize(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(
        CARRIER, "now_et",
        lambda: datetime(2026, 10, 8, 8, 0, tzinfo=ZoneInfo("America/New_York")))
    monkeypatch.setattr(CARRIER, "is_trading_day", lambda day: True)
    monkeypatch.setattr(CARRIER, "watchlist_symbols", lambda: ["US.AAA"])

    def fake_run(args, **kwargs):
        if any("pullback_stats.py" in str(a) for a in args):
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(args, 124, stdout="", stderr="timeout")

    def fake_finalize(date):
        _bridge(tmp_path, date)
        return {"returncode": 0, "result": {"finalized": True}}

    monkeypatch.setattr(CARRIER.subprocess, "run", fake_run)
    monkeypatch.setattr(CARRIER, "_finalize", fake_finalize)

    assert CARRIER.main() == 0
    event = _last(CARRIER.EVENT_PATH)
    assert event["status"] == "pipeline_timeout_finalized"
    assert event["returncode"] == 124
