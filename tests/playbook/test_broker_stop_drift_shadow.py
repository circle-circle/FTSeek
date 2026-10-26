"""P1 read-only broker STOP vs online effective SL drift contracts."""
import json

import pytest

from app.playbook import broker_stop_drift_shadow as shadow


def _row(**overrides):
    row = {
        "symbol": "US.MU",
        "direction": "LONG",
        "source_type": "PLAYBOOK_LIVE",
    }
    row.update(overrides)
    return row


class _Broker:
    def __init__(self, result=None, error=None):
        self.result = result or {
            "success": True,
            "order_status": "SUBMITTED",
            "aux_price": 99.0,
            "price": 0.0,
        }
        self.error = error
        self.calls = []

    def query_order_status(self, order_id, target_env="real"):
        self.calls.append((order_id, target_env))
        if self.error:
            raise RuntimeError(self.error)
        return self.result


@pytest.fixture(autouse=True)
def _clear_throttle(monkeypatch):
    monkeypatch.setattr(shadow, "_LAST_EMITTED", {})


def test_records_static_broker_stop_vs_tightened_online_sl(tmp_path):
    target = tmp_path / "drift.jsonl"
    broker = _Broker()

    record = shadow.record_stop_drift_shadow(
        broker,
        row=_row(),
        meta={"sl_broker_order_id": "STOP-1", "broker_stop": {"stop_price": 99.0}},
        effective_sl=101.5,
        path=target,
        now_ts=1000.0,
    )

    assert record["observation_status"] == "observed"
    assert record["broker_stop_price"] == 99.0
    assert record["drift_usd"] == -2.5
    assert record["abs_drift_bps"] == pytest.approx(246.31, abs=0.01)
    assert record["record_only"] is True
    assert broker.calls == [("STOP-1", "real")]
    assert len(target.read_text(encoding="utf-8").splitlines()) == 1


def test_throttle_is_per_symbol_and_stop_id(tmp_path):
    target = tmp_path / "drift.jsonl"
    broker = _Broker()
    kwargs = {
        "row": _row(),
        "meta": {"sl_broker_order_id": "STOP-1"},
        "effective_sl": 100.0,
        "path": target,
    }

    assert shadow.record_stop_drift_shadow(broker, now_ts=1000.0, **kwargs) is not None
    assert shadow.record_stop_drift_shadow(broker, now_ts=1100.0, **kwargs) is None
    assert shadow.record_stop_drift_shadow(broker, now_ts=1301.0, **kwargs) is not None
    assert len(broker.calls) == 2


def test_non_live_position_is_ignored(tmp_path):
    result = shadow.record_stop_drift_shadow(
        _Broker(), row=_row(source_type="PLAYBOOK_SIM"),
        meta={"sl_broker_order_id": "STOP-1"}, effective_sl=100.0,
        path=tmp_path / "drift.jsonl", now_ts=1.0,
    )
    assert result is None


def test_broker_query_error_is_recorded_as_unavailable(tmp_path):
    target = tmp_path / "drift.jsonl"
    result = shadow.record_stop_drift_shadow(
        _Broker(result={"success": False, "error": "timeout"}),
        row=_row(), meta={"sl_broker_order_id": "STOP-1"},
        effective_sl=100.0, path=target, now_ts=1.0,
    )
    assert result["observation_status"] == "unavailable"
    assert "timeout" in result["error"]
    assert len(target.read_text(encoding="utf-8").splitlines()) == 1


def test_write_failure_never_propagates(tmp_path):
    bad = tmp_path / "dir"
    bad.mkdir()
    result = shadow.record_stop_drift_shadow(
        _Broker(), row=_row(), meta={"sl_broker_order_id": "STOP-1"},
        effective_sl=100.0, path=bad, now_ts=1.0,
    )
    assert result is None
