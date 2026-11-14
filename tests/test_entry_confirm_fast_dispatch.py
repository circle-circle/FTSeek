"""RFC-204 确认后主线程提前派发契约。"""
from datetime import datetime

from app.core.scheduler_core import TradingScheduler


def _scheduler(now, monkeypatch, calls, side_effect=None):
    scheduler = TradingScheduler.__new__(TradingScheduler)
    scheduler.PLAYBOOK_AUTO_EXECUTE_INTERVAL_SEC = 180
    scheduler._playbook_auto_execute_last_run = now
    scheduler._entry_confirm_ready_generation = 0
    scheduler._entry_confirm_dispatched_generation = 0

    def fake_execute(_scheduler):
        calls.append("execute")
        if side_effect is not None:
            side_effect(scheduler)

    monkeypatch.setattr("app.playbook.pipeline.run_playbook_auto_execute", fake_execute)
    return scheduler


def test_normal_interval_is_unchanged_without_ready_confirmation(monkeypatch):
    now = datetime(2026, 11, 12, 10, 0)
    scheduler = _scheduler(now, monkeypatch, [])
    assert scheduler._maybe_auto_execute(now) is False


def test_ready_confirmation_bypasses_interval_only_once(monkeypatch):
    now = datetime(2026, 11, 12, 10, 0)
    calls = []
    scheduler = _scheduler(now, monkeypatch, calls)
    scheduler._entry_confirm_ready_generation = 1
    assert scheduler._maybe_auto_execute(now) is True
    assert scheduler._entry_confirm_dispatched_generation == 1
    assert scheduler._maybe_auto_execute(now) is False
    assert calls == ["execute"]


def test_confirmation_arriving_during_execution_is_not_lost(monkeypatch):
    now = datetime(2026, 11, 12, 10, 0)
    calls = []
    scheduler = _scheduler(now, monkeypatch, calls,
                           side_effect=lambda s: setattr(s, "_entry_confirm_ready_generation", 2))
    scheduler._entry_confirm_ready_generation = 1
    assert scheduler._maybe_auto_execute(now) is True
    assert scheduler._entry_confirm_dispatched_generation == 1
    assert scheduler._maybe_auto_execute(now) is True
    assert scheduler._entry_confirm_dispatched_generation == 2
    assert calls == ["execute", "execute"]


def test_fast_dispatch_rollback_restores_interval(monkeypatch):
    now = datetime(2026, 11, 12, 10, 0)
    scheduler = _scheduler(now, monkeypatch, [])
    scheduler._entry_confirm_ready_generation = 1
    from types import SimpleNamespace
    monkeypatch.setattr("app.core.config_container.get_config", lambda: SimpleNamespace(
        get_dotted=lambda key, default=None: False))
    assert scheduler._maybe_auto_execute(now) is False


def test_open_floor_boundary_dispatches_next_main_loop(monkeypatch):
    last = datetime(2026, 11, 12, 9, 42)
    calls = []
    scheduler = _scheduler(last, monkeypatch, calls)
    before_floor = datetime(2026, 11, 12, 9, 44, 59)
    assert scheduler._maybe_auto_execute(before_floor) is False
    at_floor = datetime(2026, 11, 12, 9, 45)
    assert scheduler._maybe_auto_execute(at_floor) is True
    assert scheduler._maybe_auto_execute(at_floor) is False
    assert calls == ["execute"]


def test_open_floor_boundary_rollback(monkeypatch):
    from types import SimpleNamespace
    last = datetime(2026, 11, 12, 9, 43)
    scheduler = _scheduler(last, monkeypatch, [])
    monkeypatch.setattr("app.core.config_container.get_config", lambda: SimpleNamespace(
        get_dotted=lambda key, default=None: False))
    assert scheduler._maybe_auto_execute(datetime(2026, 11, 12, 9, 45)) is False


def test_execution_failure_does_not_drop_ready_generation(monkeypatch):
    now = datetime(2026, 11, 12, 10, 0)
    scheduler = _scheduler(now, monkeypatch, [],
                           side_effect=lambda s: (_ for _ in ()).throw(RuntimeError("boom")))
    scheduler._entry_confirm_ready_generation = 1
    assert scheduler._maybe_auto_execute(now) is False
    assert scheduler._entry_confirm_dispatched_generation == 0
    monkeypatch.setattr("app.playbook.pipeline.run_playbook_auto_execute", lambda _: None)
    assert scheduler._maybe_auto_execute(now) is True
    assert scheduler._entry_confirm_dispatched_generation == 1
