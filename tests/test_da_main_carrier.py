"""DA main dual-season carrier schedule contract tests."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess

import pytest

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


def _run_event(status="complete"):
    return {"date": "2026-10-08", "started_at": "2026-10-08T08:00:00-04:00",
            "status": status}


def _observed_result(selected, rows):
    return {"stages": {
        "filter": {"selected_symbols": selected, "screen_batch_id": "DA-test",
                   "screen_policy_version": "test-v1"},
        "output": {"analyses": rows},
    }}


def _analysis(symbol="US.AAA", action="BUY", gate="PASS", rating="看多"):
    return {"symbol": symbol, "action": action, "rating": rating,
            "_signal_gate": {"decision": gate}}


@pytest.mark.parametrize("selected,rows,candidates,expected,zero", [
    (["US.AAA"], [_analysis()], 1, "complete", False),
    ([], [], 0, "complete", True),
    (["US.AAA"], [_analysis(action="SKIP")], 0, "complete", True),
    (["US.AAA"], [_analysis(gate="BLOCK")], 0, "complete", True),
    (["US.AAA", "US.BBB"], [_analysis()], 1, "partial", False),
    (["US.AAA"], [_analysis(rating="错误")], 0, "failed", False),
    (["US.AAA"], [_analysis()], 0, "partial", False),
])
def test_batch_observation_distinguishes_coverage_and_legal_zero(
        monkeypatch, tmp_path, selected, rows, candidates, expected, zero):
    _isolate(monkeypatch, tmp_path)
    _bridge(tmp_path, candidates=candidates)
    got = CARRIER._batch_observation(_run_event(), _observed_result(selected, rows))
    assert got["state"] == expected
    assert got["legal_zero_candidates"] is zero
    assert got["selected_count"] == len(selected)
    assert got["batch_id"] == "DA-test"
    assert got["selection_policy_version"] == "test-v1"
    if len(selected) == 2:
        assert got["missing_symbols"] == ["BBB"]


def test_same_day_old_bridge_cannot_prove_this_batch_complete(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _bridge(tmp_path)
    path = tmp_path / "opportunity_scan_2026-10-08.json"
    stale = datetime.fromisoformat(_run_event()["started_at"]).timestamp() - 1
    os.utime(path, (stale, stale))
    got = CARRIER._batch_observation(
        _run_event(), _observed_result(["US.AAA"], [_analysis()]))
    assert got["state"] == "partial"
    assert got["reason"] == "current_bridge_unverified"
    assert got["bridge_fresh"] is False
    # Legacy success criteria are deliberately unchanged.
    assert CARRIER._verify_artifacts("2026-10-08")["ok"] is True


@pytest.mark.parametrize("suffix,key", [(".raw.json", "fresh_raw_count"),
                                        (".json", "fresh_analysis_count")])
def test_fresh_files_without_current_manifest_only_prove_progress(
        monkeypatch, tmp_path, suffix, key):
    _isolate(monkeypatch, tmp_path)
    root = tmp_path / "da_checkpoint" / "2026-10-08"
    root.mkdir(parents=True)
    (root / ("US.AAA" + suffix)).write_text(json.dumps(_analysis()))
    got = CARRIER._batch_observation(_run_event("pipeline_timeout_finalized"))
    assert got["state"] == "partial"
    assert got[key] == 1
    assert got["selected_count"] is None
    assert got["publishable_count"] is None
    assert got["legal_zero_candidates"] is False
    assert got["reason"] == "current_selection_unavailable"


def test_failed_scan_is_not_legal_zero_candidates(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _bridge(tmp_path, candidates=0)
    result = _observed_result([], [])
    result["stages"]["signal"] = {"market_condition": "扫描失败: timeout"}
    got = CARRIER._batch_observation(_run_event(), result)
    assert got["state"] == "failed"
    assert got["legal_zero_candidates"] is False


def test_absent_output_is_not_explicit_empty_output(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _bridge(tmp_path, candidates=0)
    result = _observed_result([], [])
    result["stages"].pop("output")
    got = CARRIER._batch_observation(_run_event(), result)
    assert got["state"] == "unknown"
    assert got["legal_zero_candidates"] is False


@pytest.mark.parametrize("returncode,expected_rc", [(124, 0), (137, 0), (143, 0), (7, 7)])
def test_batch_observation_does_not_change_legacy_returncodes(
        monkeypatch, tmp_path, returncode, expected_rc):
    _isolate(monkeypatch, tmp_path)
    _bridge(tmp_path)
    stamp = datetime.fromisoformat(_run_event()["started_at"])
    stale = stamp.timestamp() - 1
    os.utime(tmp_path / "opportunity_scan_2026-10-08.json", (stale, stale))
    monkeypatch.setattr(CARRIER, "now_et", lambda: stamp)
    monkeypatch.setattr(CARRIER, "is_trading_day", lambda day: True)
    monkeypatch.setattr(CARRIER, "watchlist_symbols", lambda: ["US.AAA"])
    monkeypatch.setattr(CARRIER, "HEARTBEAT_SEC", 0)
    monkeypatch.setattr(CARRIER, "_finalize", lambda date: {"returncode": 1})

    def fake_run(args, **kwargs):
        rc = returncode if "--mode" in args else 0
        return subprocess.CompletedProcess(args, rc, stdout="", stderr="")

    monkeypatch.setattr(CARRIER.subprocess, "run", fake_run)
    assert CARRIER.main() == expected_rc
    event = _last(CARRIER.EVENT_PATH)
    assert event["returncode"] == returncode
    assert event["artifacts"]["ok"] is True
    assert event["batch_observation"]["state"] == "failed"


def test_observation_failure_remains_non_blocking(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)

    def fail(*args):
        raise OSError("observation unavailable")

    monkeypatch.setattr(CARRIER, "_batch_observation", fail)
    CARRIER._append_terminal_event(_run_event())
    event = _last(CARRIER.EVENT_PATH)
    assert event["status"] == "complete"
    assert event["batch_observation"]["state"] == "unknown"
    assert event["batch_observation"]["reason"] == "observation_error"


def test_unknown_gate_is_not_publishable_or_legal_zero(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _bridge(tmp_path, candidates=0)
    row = _analysis()
    row.pop("_signal_gate")
    got = CARRIER._batch_observation(_run_event(), _observed_result(["US.AAA"], [row]))
    assert got["state"] == "unknown"
    assert got["reason"] == "gate_decision_unknown"
    assert got["gate_unknown_symbols"] == ["AAA"]
    assert got["publishable_count"] == 0
    assert got["legal_zero_candidates"] is False
