import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.observability import watchdog as module
from app.core import scheduler_triggers


@pytest.fixture
def setup(monkeypatch):
    dump, send = Mock(), Mock()
    monkeypatch.setattr(module.faulthandler, "dump_traceback", dump)
    monkeypatch.setitem(sys.modules, "app.utils.critical_alerts", SimpleNamespace(send_critical=send))
    monkeypatch.setattr(scheduler_triggers, "get_market_phase", lambda: scheduler_triggers.MarketPhase.TRADING)
    w = module.Watchdog()
    w._heal_enabled = False
    w._heartbeats = {"llm_strategy_loop": 1000}
    w._thresholds = {"llm_strategy_loop": 600}
    monkeypatch.setattr(module.time, "time", lambda: 1701)
    return w, dump, send


def test_stale_captures_stack_before_notification(setup):
    w, dump, send = setup
    sequence = []
    dump.side_effect = lambda **kwargs: sequence.append("dump")
    send.side_effect = lambda *args, **kwargs: sequence.append("send")
    w._check_all()
    assert sequence == ["dump", "send"]
    dump.assert_called_once_with(all_threads=True)
    assert send.call_args.kwargs == {"async_mode": True}


def test_cooldown_limits_dumps_and_alerts(setup, monkeypatch):
    w, dump, send = setup
    w._check_all()
    w._check_all()
    dump.assert_called_once()
    send.assert_called_once()
    monkeypatch.setattr(module.time, "time", lambda: 2302)
    w._check_all()
    assert dump.call_count == send.call_count == 2


def test_stack_failure_keeps_notification(setup, caplog):
    w, dump, send = setup
    dump.side_effect = RuntimeError("stderr unavailable")
    w._check_all()
    send.assert_called_once()
    assert "卡顿线程栈记录失败" in caplog.text


@pytest.mark.parametrize("skip", ["fresh", "init", "weekend"])
def test_no_dump_when_heartbeat_check_skipped(setup, monkeypatch, skip):
    w, dump, send = setup
    if skip == "fresh":
        w._heartbeats["llm_strategy_loop"] = 1700
    elif skip == "init":
        w._started_at = 1700
    else:
        monkeypatch.setattr(scheduler_triggers, "get_market_phase", lambda: scheduler_triggers.MarketPhase.WEEKEND)
    w._check_all()
    dump.assert_not_called()
    send.assert_not_called()


def test_intraday_stale_does_not_restart(setup, monkeypatch):
    import subprocess

    w, dump, send = setup
    w._heal_enabled = True
    monkeypatch.setattr(module.time, "time", lambda: 3000)
    restart = Mock(side_effect=AssertionError("must not restart"))
    monkeypatch.setattr(subprocess, "run", restart)
    monkeypatch.delenv("WATCHDOG_AUTO_HEAL", raising=False)
    w._check_all()
    dump.assert_called_once()
    assert send.call_count == 2
    restart.assert_not_called()
