import time
from pathlib import Path
import sys
from types import SimpleNamespace

import app.playbook.eth_held_exit_shadow as shadow
import app.playbook.pipeline as pipeline


def _intent():
    return {
        "intent_id": "test-intent",
        "symbol": "US.TEST",
        "direction": "LONG",
        "target_env": "sim",
        "session": "ETH",
        "state": "HELD",
        "quantity": 39.0,
        "created_at": time.time() - 300,
    }


def _result():
    return {
        "exit_state": "HELD",
        "error": "exit_eth_partial_fill_held",
        "dealt_qty": 0.0,
        "futu_order_id": "test-order",
    }


def test_rth_takeover_releases_terminal_eth_held_intent(monkeypatch):
    monkeypatch.setattr(shadow, "_regular_market_open", lambda: True)
    monkeypatch.setattr(shadow, "_position_still_open", lambda symbol, direction: True)
    calls = {}

    class FakeDual:
        def release_exit_intent(self, intent_id, **kwargs):
            calls["intent_id"] = intent_id
            calls.update(kwargs)
            return Path("archive.json"), None

    assert pipeline._release_eth_held_exit_for_rth_takeover(
        FakeDual(), _intent(), _result()) is True
    assert calls["intent_id"] == "test-intent"
    assert calls["reason"] == "rth_intraday_takeover"
    assert calls["kind"] == "rth_intraday_takeover"
    assert calls["evidence"]["remaining_qty"] == 39.0


def test_rth_takeover_does_not_release_partial_fill(monkeypatch):
    monkeypatch.setattr(shadow, "_regular_market_open", lambda: True)
    monkeypatch.setattr(shadow, "_position_still_open", lambda symbol, direction: True)

    class FakeDual:
        def release_exit_intent(self, *args, **kwargs):
            raise AssertionError("partial fill must require manual reconciliation")

    result = _result()
    result["dealt_qty"] = 10.0
    assert pipeline._release_eth_held_exit_for_rth_takeover(
        FakeDual(), _intent(), result) is False


def test_rth_takeover_fails_closed_when_dealt_qty_is_missing(monkeypatch):
    monkeypatch.setattr(shadow, "_regular_market_open", lambda: True)
    monkeypatch.setattr(shadow, "_position_still_open", lambda symbol, direction: True)
    result = _result()
    result["dealt_qty"] = None

    class FakeDual:
        def release_exit_intent(self, *args, **kwargs):
            raise AssertionError("missing dealt_qty must fail closed")

    assert shadow.build_eth_held_rth_event(_intent(), result) is None
    assert pipeline._release_eth_held_exit_for_rth_takeover(
        FakeDual(), _intent(), result) is False


def test_resume_removes_symbol_from_blocked_after_release(monkeypatch):
    intent = _intent()
    result = _result()

    class FakeDual:
        def pending_closes(self):
            return [intent]

        def resume_close(self, intent_id, allow_submit=True):
            assert intent_id == intent["intent_id"]
            return result

        def release_exit_intent(self, intent_id, **kwargs):
            return Path("archive.json"), None

    monkeypatch.setattr(shadow, "_regular_market_open", lambda: True)
    monkeypatch.setattr(shadow, "_position_still_open", lambda symbol, direction: True)
    monkeypatch.setitem(
        sys.modules, "app.execution.dual_broker",
        SimpleNamespace(get_dual_broker=lambda: FakeDual()))
    monkeypatch.setattr(pipeline, "_book_playbook_exit_result", lambda dual, res: 0)

    def fail_alert(*args, **kwargs):
        raise AssertionError("released intent must not emit failure alert")

    monkeypatch.setattr(pipeline, "_alert_close_failure", fail_alert)
    closed, blocked, recovery_ok = pipeline._resume_playbook_exits(allow_submit=False)
    assert (closed, blocked, recovery_ok) == (0, set(), True)
