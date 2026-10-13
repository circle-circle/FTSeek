import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import scripts.extended_hours_exit_executor as exit_executor  # noqa: E402


@pytest.mark.parametrize(
    ("session", "now", "expected"),
    [
        ("premarket", datetime(2026, 10, 12, 9, 28, 49), True),
        ("premarket", datetime(2026, 10, 12, 9, 29, 0), False),
        ("afterhours", datetime(2026, 10, 12, 19, 58, 49), True),
        ("afterhours", datetime(2026, 10, 12, 19, 59, 0), False),
    ],
)
def test_eth_exit_submit_stops_before_session_boundary(monkeypatch, session, now, expected):
    monkeypatch.setenv("EXT_EXIT_LIMIT_TIMEOUT_S", "60")
    monkeypatch.setenv("EXT_EXIT_CANCEL_MARGIN_S", "10")

    assert exit_executor._in_session_window(session, now) is expected


def test_eth_exit_boundary_guard_scales_with_limit_timeout(monkeypatch):
    monkeypatch.setenv("EXT_EXIT_LIMIT_TIMEOUT_S", "20")
    monkeypatch.setenv("EXT_EXIT_CANCEL_MARGIN_S", "10")

    assert exit_executor._in_session_window(
        "premarket", datetime(2026, 10, 12, 9, 29, 20)
    ) is True
    assert exit_executor._in_session_window(
        "premarket", datetime(2026, 10, 12, 9, 29, 31)
    ) is False


def test_invalid_boundary_guard_config_fails_closed(monkeypatch):
    monkeypatch.setenv("EXT_EXIT_LIMIT_TIMEOUT_S", "not-a-number")

    assert exit_executor._in_session_window(
        "premarket", datetime(2026, 10, 12, 9, 0)
    ) is False


def test_next_open_rth_leg_is_not_blocked_by_eth_boundary_guard():
    assert exit_executor._in_session_window(
        "next_open", datetime(2026, 10, 12, 9, 44)
    ) is True
