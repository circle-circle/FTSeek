import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import app.playbook.pipeline as playbook_pipeline
import app.risk.position_exit_monitor as exit_monitor_module
from app.risk.position_exit_monitor import ExitMonitor


class FakePosition:
    def __init__(self, symbol):
        self.symbol = symbol
        self.direction = "LONG"
        self.avg_entry_price = 100.0
        self.size = 10
        self.metadata = {"source": "auto_main", "protection": "managed"}


def _monitor_with_flat_all(monkeypatch):
    monitor = ExitMonitor()
    monkeypatch.setattr(
        exit_monitor_module,
        "get_shared_kernel",
        lambda: SimpleNamespace(
            get_current_session=lambda: "normal",
            get_session_vol_ratio=lambda session: 1.0,
        ),
    )
    monkeypatch.setattr(
        playbook_pipeline, "load_playbook_open_symbols", lambda: set()
    )
    monitor._check_portfolio_drawdown = lambda positions, get_price_fn: "FLAT_ALL"
    return monitor


def test_flat_all_partial_failure_does_not_enter_cooldown(monkeypatch):
    monitor = _monitor_with_flat_all(monkeypatch)
    positions = [FakePosition("A"), FakePosition("B")]
    calls = []

    def execute_close(position, reason):
        calls.append(position.symbol)
        return position.symbol == "A"

    closed = monitor.scan_positions(
        positions,
        get_price_fn=lambda symbol: 100.0,
        execute_close_fn=execute_close,
    )
    assert [item["symbol"] for item in closed] == ["A"]
    assert monitor._flat_all_last_triggered_ts == 0.0

    monitor.scan_positions(
        [positions[1]],
        get_price_fn=lambda symbol: 100.0,
        execute_close_fn=execute_close,
    )
    assert calls == ["A", "B", "B"]


def test_flat_all_non_true_return_is_failure(monkeypatch):
    monitor = _monitor_with_flat_all(monkeypatch)
    positions = [FakePosition("A"), FakePosition("B")]

    def execute_close(position, reason):
        return None if position.symbol == "A" else True

    closed = monitor.scan_positions(
        positions,
        get_price_fn=lambda symbol: 100.0,
        execute_close_fn=execute_close,
    )
    assert [item["symbol"] for item in closed] == ["B"]
    assert monitor._flat_all_last_triggered_ts == 0.0


def test_flat_all_all_success_enters_cooldown(monkeypatch):
    monitor = _monitor_with_flat_all(monkeypatch)
    positions = [FakePosition("A"), FakePosition("B")]

    closed = monitor.scan_positions(
        positions,
        get_price_fn=lambda symbol: 100.0,
        execute_close_fn=lambda position, reason: True,
    )
    assert [item["symbol"] for item in closed] == ["A", "B"]
    assert monitor._flat_all_last_triggered_ts > 0.0
