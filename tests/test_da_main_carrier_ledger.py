"""DA main carrier schedule-ledger lifecycle contract tests (2026-11-13).

Pins the invariants that the 11-13 "ledger disappeared" investigation needed:
the ledger at ``data/shadow/da_main_schedule.jsonl`` is append-only, every
event is one durable parseable line even under concurrent writers, the
terminal event is verified after fsync, a vanished ledger is recreated with an
in-band marker, and write failures are loud + mirrored to the fallback ledger.

All tests are hermetic: no production paths, no network, no DA batch.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "ops/hermes/da_main_carrier.py"
SPEC = importlib.util.spec_from_file_location("da_main_carrier_ledger", SCRIPT)
CARRIER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CARRIER)


def _events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _isolate(monkeypatch, tmp_path: Path) -> Path:
    ledger = tmp_path / "da_main_schedule.jsonl"
    monkeypatch.setattr(CARRIER, "EVENT_PATH", ledger)
    monkeypatch.setattr(CARRIER, "BRIDGE_DIR", tmp_path)
    monkeypatch.setattr(CARRIER, "DB_PATH", tmp_path / "missing.db")
    # Per-path write tracking must start empty for every hermetic ledger.
    monkeypatch.setattr(CARRIER, "_LEDGER_WRITES", {})
    return ledger


def _event(status: str = "heartbeat", **extra) -> dict:
    return {"type": "da_main_schedule", "date": "2026-11-13",
            "started_at": "2026-11-13T08:01:08-05:00", "status": status, **extra}


def test_append_is_single_line_parseable_and_never_rewrites_history(
        monkeypatch, tmp_path):
    ledger = _isolate(monkeypatch, tmp_path)
    assert CARRIER.append_event(_event("running", pid=1)) is True
    first_bytes = ledger.read_bytes()

    assert CARRIER.append_event(_event("heartbeat", analyses_done=1)) is True

    # Old bytes are untouched: the ledger is append-only, never rewritten.
    assert ledger.read_bytes().startswith(first_bytes)
    events = _events(ledger)
    assert [e["status"] for e in events] == ["running", "heartbeat"]
    assert not (tmp_path / "da_main_schedule.jsonl.fallback").exists()


def test_concurrent_threads_never_interleave_or_lose_lines(monkeypatch, tmp_path):
    ledger = _isolate(monkeypatch, tmp_path)
    threads = [
        threading.Thread(target=CARRIER.append_event,
                         args=(_event("heartbeat", seq=[worker, i]),))
        for worker in range(24) for i in range(5)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    events = _events(ledger)
    assert len(events) == 120
    assert len({tuple(e["seq"]) for e in events}) == 120


def test_concurrent_processes_never_interleave_or_lose_lines(monkeypatch, tmp_path):
    ledger = _isolate(monkeypatch, tmp_path)
    code = (
        "import importlib.util, sys\n"
        "from pathlib import Path\n"
        "spec = importlib.util.spec_from_file_location('carrier_proc', sys.argv[1])\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        "mod.EVENT_PATH = Path(sys.argv[2])\n"
        "mod._LEDGER_WRITES = {}\n"
        "tag = sys.argv[3]\n"
        "for i in range(20):\n"
        "    mod.append_event({'type': 'da_main_schedule', 'status': 'heartbeat',\n"
        "                      'date': '2026-11-13', 'seq': [tag, i]})\n"
    )
    procs = [subprocess.Popen(
        [sys.executable, "-c", code, str(SCRIPT), str(ledger), tag],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for tag in ("a", "b", "c")]
    for proc in procs:
        _, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, err

    events = _events(ledger)
    assert len(events) == 60
    assert len({tuple(e["seq"]) for e in events}) == 60


def test_terminal_event_is_written_verified_and_last(monkeypatch, tmp_path):
    ledger = _isolate(monkeypatch, tmp_path)
    assert CARRIER.append_event(_event("running", pid=42)) is True
    CARRIER._append_terminal_event(_event("complete", finished_at="2026-11-13T08:26:29-05:00"))

    events = _events(ledger)
    assert events[-1]["status"] == "complete"
    assert events[-1]["finished_at"] == "2026-11-13T08:26:29-05:00"
    assert events[-1]["batch_observation"]["state"] in ("unknown", "partial", "failed")
    assert not (tmp_path / "da_main_schedule.jsonl.fallback").exists()


def test_heartbeat_lifecycle_emits_stage_events_then_stops(monkeypatch, tmp_path):
    ledger = _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(CARRIER, "_checkpoint_count", lambda date: 1)
    monkeypatch.setattr(CARRIER, "HEARTBEAT_SEC", 60)
    monkeypatch.setattr(CARRIER, "_deadline_seconds", lambda stamp: 1800.0)

    class _StopAfterOneBody:
        def __init__(self):
            self.waits = 0

        def wait(self, timeout):
            self.waits += 1
            return self.waits > 1

    base = {"type": "da_main_schedule", "date": "2026-11-13",
            "started_at": "2026-11-13T08:01:08-05:00"}
    CARRIER._heartbeat("2026-11-13", base, _StopAfterOneBody())

    statuses = [e["status"] for e in _events(ledger)]
    assert statuses == ["heartbeat", "first_checkpoint"]


def test_terminal_event_stops_live_heartbeat_before_final_line(
        monkeypatch, tmp_path):
    ledger = _isolate(monkeypatch, tmp_path)
    stop = threading.Event()

    def worker():
        seq = 0
        while not stop.wait(0.005):
            CARRIER.append_event(_event("heartbeat", seq=seq))
            seq += 1

    thread = threading.Thread(target=worker, daemon=True)
    monkeypatch.setattr(CARRIER, "_HEARTBEAT_STOP", stop)
    monkeypatch.setattr(CARRIER, "_HEARTBEAT_THREAD", thread)
    thread.start()

    CARRIER._append_terminal_event(
        _event("complete", finished_at="2026-11-13T08:26:29-05:00"))

    assert not thread.is_alive()
    events = _events(ledger)
    assert events[-1]["status"] == "complete"
    assert all(e["status"] == "heartbeat" for e in events[:-1])


def test_vanished_ledger_is_recreated_with_in_band_marker(
        monkeypatch, tmp_path, capsys):
    ledger = _isolate(monkeypatch, tmp_path)
    assert CARRIER.append_event(_event("running")) is True
    ledger.unlink()  # simulate an external deleter between two append calls

    assert CARRIER.append_event(_event("complete")) is True

    events = _events(ledger)
    assert [e["status"] for e in events] == ["ledger_missing_recreated", "complete"]
    marker = events[0]
    assert marker["path"] == str(ledger)
    assert "vanished" in marker["note"]
    assert "ledger vanished mid-run" in capsys.readouterr().err


def test_write_failure_is_loud_and_mirrored_to_fallback(
        monkeypatch, tmp_path, capsys):
    ledger = _isolate(monkeypatch, tmp_path)
    fallback = tmp_path / "da_main_schedule.jsonl.fallback"
    real_append = CARRIER._append_bytes

    def failing_primary(path, payload):
        if Path(path) == ledger:
            raise OSError("simulated disk failure")
        return real_append(path, payload)

    monkeypatch.setattr(CARRIER, "_append_bytes", failing_primary)

    assert CARRIER.append_event(_event("complete")) is False

    captured = capsys.readouterr()
    assert "schedule event write failed (non-blocking)" in captured.out
    assert "schedule event write failed (non-blocking)" in captured.err
    assert "simulated disk failure" in captured.out
    mirrored = _events(fallback)
    assert len(mirrored) == 1
    assert mirrored[0]["status"] == "complete"
    assert mirrored[0]["ledger_fallback_for"] == str(ledger)


def test_terminal_verification_failure_is_loud_and_mirrored(
        monkeypatch, tmp_path, capsys):
    ledger = _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(CARRIER, "_persisted", lambda path, payload: False)

    CARRIER._append_terminal_event(_event("complete", finished_at="2026-11-13T08:26:29-05:00"))

    captured = capsys.readouterr()
    assert "event missing from ledger tail after fsync" in captured.out
    assert "event missing from ledger tail after fsync" in captured.err
    mirrored = _events(tmp_path / "da_main_schedule.jsonl.fallback")
    assert mirrored[-1]["status"] == "complete"
    assert mirrored[-1]["batch_observation"]["reason"] in (
        "observation_error", "current_selection_unavailable")


def test_observation_failure_still_persists_verified_terminal_event(
        monkeypatch, tmp_path):
    ledger = _isolate(monkeypatch, tmp_path)

    def explode(*args):
        raise OSError("observation unavailable")

    monkeypatch.setattr(CARRIER, "_batch_observation", explode)
    CARRIER._append_terminal_event(_event("complete"))
    event = _events(ledger)[-1]
    assert event["status"] == "complete"
    assert event["batch_observation"] == {"state": "unknown",
                                          "reason": "observation_error",
                                          "error": "OSError"}
    assert not (tmp_path / "da_main_schedule.jsonl.fallback").exists()


def test_existing_history_survives_a_new_carrier_run(monkeypatch, tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    ledger = _isolate(monkeypatch, tmp_path)
    historical = json.dumps({"type": "da_main_schedule", "date": "2026-11-12",
                             "status": "complete"}, ensure_ascii=False) + "\n"
    ledger.write_text(historical, encoding="utf-8")
    monkeypatch.setattr(
        CARRIER, "now_et",
        lambda: datetime(2026, 11, 13, 10, 30, tzinfo=ZoneInfo("America/New_York")))
    monkeypatch.setattr(CARRIER, "is_trading_day", lambda day: True)

    assert CARRIER.main() == 0

    assert ledger.read_text(encoding="utf-8").startswith(historical)
    events = _events(ledger)
    assert events[0]["date"] == "2026-11-12"
    assert events[-1]["status"] == "skipped_outside_window"


def test_carrier_source_never_truncates_or_deletes_the_ledger():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "O_APPEND" in source
    assert "os.fsync" in source
    assert "_stop_heartbeat()" in source
    assert "unlink(" not in source
    assert "O_TRUNC" not in source
    assert 'EVENT_PATH.open("w' not in source
    assert "EVENT_PATH.write_text" not in source