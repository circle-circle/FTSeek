import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.extended_hours_exit_cron import (
    collector_command,
    get_exit_cron_session,
    run_once,
)


def test_next_open_window_is_bound_and_exclusive():
    assert get_exit_cron_session(datetime(2026, 9, 18, 9, 30)) == "next_open"
    assert get_exit_cron_session(datetime(2026, 9, 18, 9, 35)) == "next_open"
    assert get_exit_cron_session(datetime(2026, 9, 18, 9, 45)) is None
    assert get_exit_cron_session(datetime(2026, 9, 18, 9, 46)) is None


def test_next_open_reuses_premarket_collector(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "scripts.extended_hours_exit_cron.get_exit_cron_session",
        lambda now=None: "next_open",
    )
    monkeypatch.setattr(
        "scripts.extended_hours_exit_cron.LOCK_FILE",
        tmp_path / "exit_cron.lock",
    )

    calls = []
    monkeypatch.setattr(
        "scripts.extended_hours_exit_cron.subprocess.run",
        lambda command, **kwargs: calls.append(command)
        or type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
    )

    result = run_once(dry=False)
    assert result["ok"] is True
    assert result["session"] == "next_open"
    assert calls[0][1].endswith("premarket_exit_shadow.py")
    assert collector_command("next_open")[1].endswith("premarket_exit_shadow.py")
