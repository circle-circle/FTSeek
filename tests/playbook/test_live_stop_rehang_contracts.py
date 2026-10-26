"""P0-1 one-shot LIVE protective STOP rehang contracts."""
import json
import sqlite3

from app.playbook import pipeline


def _db(tmp_path):
    path = tmp_path / "live.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE trade_records ("
        "id INTEGER PRIMARY KEY, symbol TEXT, direction TEXT, quantity REAL, "
        "source_type TEXT, metadata_json TEXT)"
    )
    connection.execute(
        "CREATE TABLE open_positions ("
        "symbol TEXT PRIMARY KEY, account_type TEXT, owner_strategy TEXT, owner TEXT, "
        "sl_broker_order_id TEXT)"
    )
    connection.execute(
        "INSERT INTO trade_records VALUES "
        "(1, 'US.MU', 'LONG', 9, 'PLAYBOOK_LIVE', ?)",
        (json.dumps({
            "sl_broker_order_id": "STOP-1",
            "stop_price": 99.0,
            "stop_loss": 98.0,
        }),),
    )
    connection.execute(
        "INSERT INTO open_positions VALUES "
        "('US.MU', 'live', 'playbook_live', 'auto', 'STOP-1')"
    )
    connection.commit()
    connection.row_factory = sqlite3.Row
    return path, connection


def _row():
    return {
        "id": 1,
        "symbol": "US.MU",
        "direction": "LONG",
        "quantity": 9,
        "source_type": "PLAYBOOK_LIVE",
    }


class _Broker:
    def __init__(self, *, positions=None, orders=None, position_error="", order_error="",
                 place_result=None):
        self.positions = positions if positions is not None else [
            {"code": "US.MU", "qty": 9, "position_side": "LONG"}
        ]
        self.orders = orders or []
        self.position_error = position_error
        self.order_error = order_error
        self.place_result = place_result or {"success": True, "futu_order_id": "STOP-2"}
        self.position_queries = 0
        self.order_queries = 0
        self.place_calls = []

    def query_positions(self, target_env="real", *, refresh_cache=False):
        assert target_env == "real"
        assert refresh_cache is True
        self.position_queries += 1
        if self.position_error:
            return {"success": False, "positions": [], "error": self.position_error}
        return {"success": True, "positions": self.positions, "error": ""}

    def query_symbol_orders(self, symbol, target_env="real"):
        assert symbol == "US.MU" and target_env == "real"
        self.order_queries += 1
        if self.order_error:
            return {"success": False, "orders": [], "error": self.order_error}
        return {"success": True, "orders": self.orders, "error": ""}

    def place_protective_stop(self, symbol, qty, direction, stop_price, target_env="real"):
        self.place_calls.append({
            "symbol": symbol,
            "qty": qty,
            "direction": direction,
            "stop_price": stop_price,
            "target_env": target_env,
        })
        return self.place_result


def _patch_halt(monkeypatch):
    calls = []

    def fake_halt(symbol, reason, detail, stop_order_id=""):
        calls.append((symbol, reason, detail, stop_order_id))
        return {"action": "halted", "reason": reason, "detail": detail}

    monkeypatch.setattr(pipeline, "_halt_live_stop_protection_gap", fake_halt)
    return calls


def test_explicit_close_failure_rehangs_once_and_persists_receipt(tmp_path, monkeypatch):
    _, conn = _db(tmp_path)
    broker = _Broker()
    halted = _patch_halt(monkeypatch)
    meta = json.loads(conn.execute(
        "SELECT metadata_json FROM trade_records WHERE id=1"
    ).fetchone()[0])

    result = pipeline._handle_live_rule_exit_failure(
        broker, conn, _row(), meta,
        {"success": False, "error": "exit_evidence_invalid"},
    )

    assert result["action"] == "rehung"
    assert result["futu_order_id"] == "STOP-2"
    assert broker.place_calls == [{
        "symbol": "US.MU", "qty": 9.0, "direction": "LONG",
        "stop_price": 99.0, "target_env": "real",
    }]
    saved = json.loads(conn.execute(
        "SELECT metadata_json FROM trade_records WHERE id=1"
    ).fetchone()[0])
    position_stop = conn.execute(
        "SELECT sl_broker_order_id FROM open_positions WHERE symbol='US.MU'"
    ).fetchone()[0]
    assert saved["stop_rehang_attempted"] is True
    assert saved["stop_rehang_count"] == 1
    assert saved["sl_broker_order_id"] == "STOP-2"
    assert saved["stop_rehang_replaced_order_id"] == "STOP-1"
    assert position_stop == "STOP-2"
    assert halted == []
    conn.close()


def test_pending_close_outcome_halts_without_any_sell_order(tmp_path, monkeypatch):
    _, conn = _db(tmp_path)
    broker = _Broker()
    halted = _patch_halt(monkeypatch)
    meta = json.loads(conn.execute(
        "SELECT metadata_json FROM trade_records WHERE id=1"
    ).fetchone()[0])

    result = pipeline._handle_live_rule_exit_failure(
        broker, conn, _row(), meta,
        {"success": False, "error": "exit_order_evidence_pending"},
    )

    assert result["action"] == "halted"
    assert halted and halted[0][1] == "close_outcome_unknown"
    assert broker.place_calls == []
    assert broker.position_queries == 0
    conn.close()


def test_active_close_order_blocks_rehang_and_halts(tmp_path, monkeypatch):
    _, conn = _db(tmp_path)
    broker = _Broker(orders=[{
        "order_id": "OTHER-SELL",
        "order_status": "SUBMITTED",
        "trd_side": "SELL",
    }])
    halted = _patch_halt(monkeypatch)
    meta = json.loads(conn.execute(
        "SELECT metadata_json FROM trade_records WHERE id=1"
    ).fetchone()[0])

    result = pipeline._handle_live_rule_exit_failure(
        broker, conn, _row(), meta,
        {"success": False, "error": "exit_evidence_invalid"},
    )

    assert result["action"] == "halted"
    assert halted[0][1] == "active_close_order_present"
    assert broker.place_calls == []
    conn.close()


def test_changed_or_partial_broker_position_blocks_rehang(tmp_path, monkeypatch):
    _, conn = _db(tmp_path)
    broker = _Broker(positions=[{
        "code": "US.MU", "qty": 4, "position_side": "LONG",
    }])
    halted = _patch_halt(monkeypatch)
    meta = json.loads(conn.execute(
        "SELECT metadata_json FROM trade_records WHERE id=1"
    ).fetchone()[0])

    result = pipeline._handle_live_rule_exit_failure(
        broker, conn, _row(), meta,
        {"success": False, "error": "exit_terminal_shortfall"},
    )

    assert result["action"] == "halted"
    assert halted[0][1] == "position_changed_or_ambiguous"
    assert broker.place_calls == []
    conn.close()


def test_rehang_guard_is_durable_across_retry(tmp_path, monkeypatch):
    _, conn = _db(tmp_path)
    broker = _Broker()
    halted = _patch_halt(monkeypatch)
    meta = json.loads(conn.execute(
        "SELECT metadata_json FROM trade_records WHERE id=1"
    ).fetchone()[0])

    first = pipeline._handle_live_rule_exit_failure(
        broker, conn, _row(), meta,
        {"success": False, "error": "exit_evidence_invalid"},
    )
    second = pipeline._handle_live_rule_exit_failure(
        broker, conn, _row(), meta,
        {"success": False, "error": "exit_evidence_invalid"},
    )

    assert first["action"] == "rehung"
    assert second["action"] == "halted"
    assert second["reason"] == "stop_rehang_already_attempted"
    assert halted[-1][1] == "stop_rehang_already_attempted"
    assert len(broker.place_calls) == 1
    conn.close()


def test_crash_after_guard_persist_halts_instead_of_silent_retry(tmp_path, monkeypatch):
    _, conn = _db(tmp_path)
    broker = _Broker()
    halted = _patch_halt(monkeypatch)
    meta = json.loads(conn.execute(
        "SELECT metadata_json FROM trade_records WHERE id=1"
    ).fetchone()[0])
    meta.update({
        "stop_rehang_attempted": True,
        "stop_rehang_count": 1,
        "stop_rehang_at": "2026-10-25T12:00:00+00:00",
    })

    result = pipeline._handle_live_rule_exit_failure(
        broker, conn, _row(), meta,
        {"success": False, "error": "exit_evidence_invalid"},
    )

    assert result["action"] == "halted"
    assert halted[0][1] == "stop_rehang_already_attempted"
    assert broker.place_calls == []
    conn.close()


def test_rehang_failure_halts_new_live(tmp_path, monkeypatch):
    _, conn = _db(tmp_path)
    broker = _Broker(place_result={"success": False, "error": "broker rejected"})
    halted = _patch_halt(monkeypatch)
    meta = json.loads(conn.execute(
        "SELECT metadata_json FROM trade_records WHERE id=1"
    ).fetchone()[0])

    result = pipeline._handle_live_rule_exit_failure(
        broker, conn, _row(), meta,
        {"success": False, "error": "exit_evidence_invalid"},
    )

    assert result["action"] == "halted"
    assert halted[0][1] == "stop_rehang_failed"
    assert len(broker.place_calls) == 1
    conn.close()


class _StopStatusBroker:
    def query_order_status(self, order_id, target_env="real"):
        assert order_id == "STOP-1" and target_env == "real"
        return {
            "success": True,
            "order_id": "STOP-1",
            "code": "US.MU",
            "trd_side": "SELL",
            "order_type": "STOP",
            "order_status": "FILLED_PART",
            "qty": 9.0,
            "dealt_qty": 4.0,
            "remark": "pb_stop_US.MU_real",
        }

    def query_order_fill(self, order_id, target_env="real"):
        return 99.0, 4.0


def test_partial_stop_fill_only_emits_aggregate_alert(monkeypatch):
    alerts = []
    monkeypatch.setattr(
        pipeline, "_alert_broker_stop_partial_fill",
        lambda *args: alerts.append(args),
    )
    meta = {"sl_broker_order_id": "STOP-1"}

    release = pipeline._release_live_broker_stop_for_rule_exit(
        _StopStatusBroker(), object(), _row(), meta,
    )

    assert release == {"action": "hold", "error": "broker_stop_fill_reconcile_pending"}
    assert alerts == [("US.MU", "STOP-1", 4.0, 9.0, "LONG")]


def test_protection_gap_alerts_once_and_halts_without_sell(monkeypatch):
    pushes = []

    class _Hub:
        def push_text(self, *args, **kwargs):
            pushes.append((args, kwargs))

    monkeypatch.setattr(pipeline, "_LIVE_GUARD_ALERTED", set())
    monkeypatch.setattr("app.push.get_push_hub", lambda: _Hub())
    halts = []
    monkeypatch.setattr(
        "app.playbook.live_arm_gate.halt_live_new_positions",
        lambda reason: halts.append(reason) or {"halted": True, "reason": reason},
    )

    first = pipeline._halt_live_stop_protection_gap(
        "US.MU", "close_outcome_unknown", "pending", "STOP-1",
    )
    second = pipeline._halt_live_stop_protection_gap(
        "US.MU", "close_outcome_unknown", "pending again", "STOP-1",
    )

    assert first["action"] == "halted" and second["action"] == "halted"
    assert len(pushes) == 1
    assert len(halts) == 2
    assert halts[0].startswith("broker_stop_gap:US.MU:")
