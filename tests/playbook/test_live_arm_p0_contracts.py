"""P0 contracts for SIM/LIVE capital, risk, manual holds, and STOP protection."""

import inspect
import json
import sqlite3
from pathlib import Path

import pytest

import app.risk.capital_safety as capital_safety_module
from app.core import paths as core_paths
from app.coordinator.manual_hold_registry import (
    ManualHoldRegistry,
    manual_hold_registry,
)
from app.playbook.live_arm_gate import (
    evaluate_live_arm_gate,
    halt_live_new_positions,
    read_live_arm_halt,
)
from app.playbook import pipeline
from app.playbook.playbook_service import PlaybookService
from app.storage.trade_store import TradeStore


_ARM_CONFIG = {
    "enabled": True,
    "mode": "engineering_probe",
    "max_order_qty": 1,
    "max_open_positions": 1,
    "session": "RTH",
    "probe": {
        "authorized": True,
        "authorization_id": "TEST-AUTH",
        "direction": "LONG",
    },
}


def _gate_kwargs():
    return {
        "allow_real_trading": True,
        "real_account_id": "123456789",
        "lane_db_name": "ftseek_us_live",
        "instruction_direction": "BUY",
        "market_phase": "TRADING",
        "open_live_positions": 0,
    }


def test_capital_safety_live_arm_has_its_own_ledger(monkeypatch, tmp_path):
    live_ledger = tmp_path / "ftseek_us_live.db"
    created = []

    class FakeCapitalSafety:
        def __init__(self, db_path=None, initial_capital=None, trade_env=1):
            self.db_path = Path(db_path)
            self.trade_env = trade_env
            created.append(self)

    monkeypatch.setattr(capital_safety_module, "_capital_real_instance", None)
    monkeypatch.setattr(
        "app.core.paths.get_trade_records_db_for_arm", lambda target_env: live_ledger
    )
    monkeypatch.setattr(capital_safety_module, "CapitalSafetySystem", FakeCapitalSafety)

    live = capital_safety_module.get_capital_safety_system_for_arm("real")
    same_live = capital_safety_module.get_capital_safety_system_for_arm("live")

    assert live is same_live
    assert live.db_path == live_ledger
    assert live.trade_env == 0
    assert len(created) == 1


def test_manual_hold_registry_keeps_same_symbol_in_both_arms():
    saved = dict(manual_hold_registry._holdings)
    manual_hold_registry._holdings.clear()
    try:
        manual_hold_registry.register("US.MU", "LONG", 1, "sim auto", account="sim")
        manual_hold_registry.register("US.MU", "LONG", 1, "real manual", account="real")

        assert manual_hold_registry.is_auto_blocked("US.MU", target_env="sim") is True
        assert manual_hold_registry.is_auto_blocked("US.MU", target_env="real") is True

        manual_hold_registry.unregister("US.MU", account="sim")
        assert manual_hold_registry.is_auto_blocked("US.MU", target_env="sim") is False
        assert manual_hold_registry.is_auto_blocked("US.MU", target_env="real") is True
    finally:
        manual_hold_registry._holdings.clear()
        manual_hold_registry._holdings.update(saved)


def test_manual_hold_ssot_fallback_reads_live_ledger(monkeypatch, tmp_path):
    live_ledger = tmp_path / "ftseek_us_live.db"
    connection = sqlite3.connect(live_ledger)
    connection.execute(
        "CREATE TABLE open_positions ("
        "symbol TEXT PRIMARY KEY, owner TEXT, account_type TEXT, size REAL)"
    )
    connection.execute(
        "INSERT INTO open_positions VALUES ('US.MU', 'manual', 'real', 10)"
    )
    connection.commit()
    connection.close()

    monkeypatch.setattr(
        ManualHoldRegistry, "_ssot_cache", {"ts": 0.0, "account": "", "symbols": set()}
    )
    monkeypatch.setattr(
        "app.core.paths.get_trade_records_db_for_arm", lambda target_env: live_ledger
    )

    assert manual_hold_registry.is_auto_blocked("US.MU", target_env="real") is True


def test_halt_state_blocks_live_gate(monkeypatch, tmp_path):
    halt_file = tmp_path / "live_arm_halt.json"
    monkeypatch.setattr(
        "app.playbook.live_arm_gate._HALT_FILE", halt_file
    )

    state = halt_live_new_positions("broker STOP failed")
    assert state["halted"] is True
    assert read_live_arm_halt()["reason"] == "broker STOP failed"

    decision = evaluate_live_arm_gate(_ARM_CONFIG, halted=True, **_gate_kwargs())
    assert decision["allowed"] is False
    assert "LIVE_NEW_POSITIONS_HALTED" in {
        reason["code"] for reason in decision["reasons"]
    }


def test_live_stop_failure_is_fail_close_and_halt_new_live():
    source = inspect.getsource(PlaybookService._execute_via_dual_broker_inner)

    assert "LIVE_MANUAL_HOLD_CHECK_FAIL_CLOSE" in source
    assert "_stop_result.get(\"success\")" in source
    assert "futu_order_id" in source
    assert "halt_live_new_positions" in source
    assert "LIVE_STOP_FAIL_CLOSE" in source
    assert "stop_protection_failed" in source
    assert "既有仓位不平仓" in source


def _broker_stop_reconciliation_ledger(path: Path):
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE trade_records ("
        "id INTEGER PRIMARY KEY, symbol TEXT, direction TEXT, entry_time TEXT, "
        "exit_time TEXT, entry_price REAL, exit_price REAL, quantity INTEGER, "
        "futu_order_id TEXT, realized_pnl REAL, pnl_pct REAL, holding_hours REAL, "
        "exit_reason TEXT, "
        "close_futu_order_id TEXT, metadata_json TEXT, source_type TEXT, "
        "mfe_pct REAL, mae_pct REAL, max_drawdown REAL)"
    )
    connection.execute(
        "CREATE TABLE open_positions ("
        "symbol TEXT PRIMARY KEY, direction TEXT, size REAL, avg_entry_price REAL, "
        "entry_time TEXT, entry_broker_order_id TEXT, sl_broker_order_id TEXT, "
        "sync_status TEXT)"
    )
    metadata = (
        '{"playbook_instruction_id": 9001, "stop_loss": 99.0, '
        '"sl_broker_order_id": "STOP-1"}'
    )
    connection.execute(
        "INSERT INTO trade_records VALUES "
        "(1, 'US.MU', 'LONG', '2026-10-06T09:35:00-04:00', '', 100.0, 0, 1, "
        "'ENTRY-1', 0, 0, 0, NULL, NULL, ?, 'PLAYBOOK_LIVE', 0, 0, 0)",
        (metadata,),
    )
    connection.execute(
        "INSERT INTO open_positions VALUES "
        "('US.MU', 'LONG', 1, 100.0, '2026-10-06T09:35:00-04:00', "
        "'ENTRY-1', 'STOP-1', 'synced')"
    )
    connection.commit()
    connection.close()


def test_broker_stop_fill_is_reconciled_without_ghost_position(tmp_path, monkeypatch):
    ledger = tmp_path / "live.db"
    _broker_stop_reconciliation_ledger(ledger)
    monkeypatch.setattr("app.push.trade_exit", lambda *args, **kwargs: None)

    class FakeDualBroker:
        def query_order_fill(self, order_id, target_env="real"):
            assert order_id == "STOP-1"
            assert target_env == "real"
            return 99.0, 1.0

    connection = sqlite3.connect(ledger)
    connection.row_factory = sqlite3.Row
    row = dict(connection.execute("SELECT * FROM trade_records WHERE id=1").fetchone())
    meta = json.loads(row["metadata_json"])

    booked = pipeline._reconcile_broker_stop_exit(
        FakeDualBroker(), connection, row, meta
    )

    assert booked is True
    trade = connection.execute(
        "SELECT exit_price, exit_time, exit_reason, close_futu_order_id, metadata_json "
        "FROM trade_records WHERE id=1"
    ).fetchone()
    assert trade["exit_price"] == 99.0
    assert trade["exit_reason"] == "SL_BROKER_STOP"
    assert trade["close_futu_order_id"] == "STOP-1"
    reconciled = json.loads(trade["metadata_json"])
    assert reconciled["broker_stop_reconciled"] is True
    assert reconciled["exit_price_source"] == "broker_stop_reconciled"
    assert connection.execute("SELECT COUNT(*) FROM open_positions").fetchone()[0] == 0
    connection.close()


def test_partial_broker_stop_fill_is_not_booked(tmp_path):
    ledger = tmp_path / "live.db"
    _broker_stop_reconciliation_ledger(ledger)

    class FakeDualBroker:
        def query_order_fill(self, order_id, target_env="real"):
            return 99.0, 0.0

    connection = sqlite3.connect(ledger)
    connection.row_factory = sqlite3.Row
    row = dict(connection.execute("SELECT * FROM trade_records WHERE id=1").fetchone())
    meta = json.loads(row["metadata_json"])

    booked = pipeline._reconcile_broker_stop_exit(
        FakeDualBroker(), connection, row, meta
    )

    assert booked is False
    assert connection.execute(
        "SELECT exit_time FROM trade_records WHERE id=1"
    ).fetchone()[0] == ""
    assert connection.execute("SELECT COUNT(*) FROM open_positions").fetchone()[0] == 1
    connection.close()


class _StopReleaseBroker:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.cancels = []

    def query_order_status(self, order_id, target_env="real"):
        assert order_id == "STOP-1" and target_env == "real"
        if len(self.statuses) > 1:
            return self.statuses.pop(0)
        return self.statuses[0]

    def cancel_order(self, order_id, target_env="real"):
        assert order_id == "STOP-1" and target_env == "real"
        self.cancels.append(order_id)
        return {"success": True, "error": ""}


def _stop_release_row(**overrides):
    row = {
        "source_type": "PLAYBOOK_LIVE",
        "symbol": "US.MU",
        "direction": "LONG",
        "quantity": 1,
    }
    row.update(overrides)
    return row


def _stop_status(**overrides):
    status = {
        "success": True,
        "order_id": "STOP-1",
        "code": "US.MU",
        "trd_side": "SELL",
        "order_type": "STOP",
        "order_status": "WAITING_SUBMIT",
        "qty": 1.0,
        "dealt_qty": 0.0,
        "remark": "pb_stop_US.MU_real",
    }
    status.update(overrides)
    return status


def test_live_rule_exit_releases_own_stop_after_terminal_cancel(monkeypatch):
    monkeypatch.setattr(pipeline.time, "sleep", lambda seconds: None)
    broker = _StopReleaseBroker([
        _stop_status(),
        _stop_status(order_status="CANCELLED_ALL"),
    ])

    release = pipeline._release_live_broker_stop_for_rule_exit(
        broker, object(), _stop_release_row(), {"sl_broker_order_id": "STOP-1"}
    )

    assert release == {"action": "proceed"}
    assert broker.cancels == ["STOP-1"]


def test_live_rule_exit_does_not_cancel_unmatched_stop():
    broker = _StopReleaseBroker([
        _stop_status(order_id="MANUAL-STOP", remark="manual"),
    ])

    release = pipeline._release_live_broker_stop_for_rule_exit(
        broker, object(), _stop_release_row(), {"sl_broker_order_id": "STOP-1"}
    )

    assert release["action"] == "hold"
    assert release["error"] == "broker_stop_identity_mismatch"
    assert broker.cancels == []


def test_live_rule_exit_holds_when_cancel_never_reaches_terminal(monkeypatch):
    monkeypatch.setattr(pipeline.time, "sleep", lambda seconds: None)
    broker = _StopReleaseBroker(
        [_stop_status()] + [_stop_status(order_status="CANCELLING_ALL")] * 20
    )

    release = pipeline._release_live_broker_stop_for_rule_exit(
        broker, object(), _stop_release_row(), {"sl_broker_order_id": "STOP-1"}
    )

    assert release["action"] == "hold"
    assert release["error"] == "broker_stop_cancel_pending"
    assert broker.cancels == ["STOP-1"]


def test_live_rule_exit_books_raced_stop_fill_instead_of_closing_twice(monkeypatch):
    broker = _StopReleaseBroker([
        _stop_status(order_status="FILLED_ALL", dealt_qty=1.0),
    ])
    connection = object()
    row = _stop_release_row()
    meta = {"sl_broker_order_id": "STOP-1"}
    reconciled = []

    def fake_reconcile(dual, conn, reconcile_row, reconcile_meta):
        reconciled.append((dual, conn, reconcile_row, reconcile_meta))
        return True

    monkeypatch.setattr(pipeline, "_reconcile_broker_stop_exit", fake_reconcile)

    release = pipeline._release_live_broker_stop_for_rule_exit(
        broker, connection, row, meta
    )

    assert release == {"action": "reconciled"}
    assert reconciled == [(broker, connection, row, meta)]
    assert broker.cancels == []


def test_all_live_close_paths_release_broker_stop_before_close():
    monitor_source = inspect.getsource(pipeline.monitor_playbook_exits)
    eod_source = inspect.getsource(pipeline._force_close_orphaned_sim_positions)

    assert monitor_source.count("_release_live_broker_stop_for_rule_exit") == 2
    assert "_release_live_broker_stop_for_rule_exit" in eod_source


def test_open_position_entry_and_stop_order_ids_have_separate_semantics(tmp_path):
    store = TradeStore(str(tmp_path / "order-ids.db"))
    store.upsert_position_simple(
        "US.MU", "LONG", 1, 100.0, futu_order_id="ENTRY-1"
    )

    connection = store._get_conn()
    row = connection.execute(
        "SELECT entry_broker_order_id, sl_broker_order_id FROM open_positions "
        "WHERE symbol='US.MU'"
    ).fetchone()
    connection.close()
    assert row == ("ENTRY-1", None)

    connection = store._get_conn()
    connection.execute(
        "UPDATE open_positions SET sl_broker_order_id='STOP-1' WHERE symbol='US.MU'"
    )
    connection.commit()
    connection.close()

    store.upsert_position_simple(
        "US.MU", "LONG", 1, 101.0, futu_order_id="ENTRY-2"
    )
    connection = store._get_conn()
    row = connection.execute(
        "SELECT entry_broker_order_id, sl_broker_order_id FROM open_positions "
        "WHERE symbol='US.MU'"
    ).fetchone()
    connection.close()
    assert row == ("ENTRY-2", "STOP-1")


def test_trade_store_migrates_legacy_entry_order_ids(tmp_path):
    db_path = tmp_path / "legacy-order-ids.db"
    store = TradeStore(str(db_path))
    connection = store._get_conn()
    connection.execute(
        "INSERT INTO trade_records "
        "(trade_date, symbol, direction, futu_order_id, created_at) "
        "VALUES ('2026-10-05', 'US.MU', 'LONG', 'ENTRY-LEGACY', "
        "'2026-10-05T09:30:00-04:00')"
    )
    connection.execute(
        "INSERT INTO open_positions "
        "(symbol, direction, size, avg_entry_price, account_type, "
        "sl_broker_order_id, updated_at) "
        "VALUES ('US.MU', 'LONG', 1, 100.0, 'simulate', 'ENTRY-LEGACY', "
        "'2026-10-05T09:30:00-04:00')"
    )
    connection.commit()
    connection.close()

    TradeStore._instances.pop(str(db_path), None)
    TradeStore(str(db_path))
    connection = sqlite3.connect(db_path)
    row = connection.execute(
        "SELECT entry_broker_order_id, sl_broker_order_id FROM open_positions "
        "WHERE symbol='US.MU'"
    ).fetchone()
    connection.close()

    assert row == ("ENTRY-LEGACY", None)


def test_live_stop_receipt_updates_real_position_columns(monkeypatch, tmp_path):
    ledger = tmp_path / "ftseek_us_live.db"
    connection = sqlite3.connect(ledger)
    connection.execute(
        "CREATE TABLE trade_records ("
        "id INTEGER PRIMARY KEY, source_type TEXT, futu_order_id TEXT, metadata_json TEXT)"
    )
    connection.execute(
        "INSERT INTO trade_records VALUES (1, 'PLAYBOOK_LIVE', 'ENTRY-1', '{}')"
    )
    connection.execute(
        "CREATE TABLE open_positions ("
        "symbol TEXT PRIMARY KEY, account_type TEXT, owner_strategy TEXT, owner TEXT,"
        "entry_broker_order_id TEXT, sl_broker_order_id TEXT)"
    )
    connection.execute(
        "INSERT INTO open_positions VALUES "
        "('US.MU', 'live', 'playbook_live', 'auto', NULL, NULL)"
    )
    connection.commit()
    connection.close()

    monkeypatch.setattr(
        core_paths, "get_trade_records_db_for_arm", lambda target_env: ledger
    )

    class FakePlaybookDB:
        db_name = "ftseek_us_live"

        def ensure_tables(self):
            pass

    service = PlaybookService(FakePlaybookDB())
    service._record_live_stop_receipt("US.MU", "ENTRY-1", "STOP-1", 99.0, 1)

    connection = sqlite3.connect(ledger)
    position = connection.execute(
        "SELECT entry_broker_order_id, sl_broker_order_id FROM open_positions"
    ).fetchone()
    metadata = connection.execute(
        "SELECT metadata_json FROM trade_records WHERE id=1"
    ).fetchone()[0]
    connection.close()

    assert position == ("ENTRY-1", "STOP-1")
    assert '"entry_broker_order_id": "ENTRY-1"' in metadata
    assert '"sl_broker_order_id": "STOP-1"' in metadata

    connection = sqlite3.connect(ledger)
    connection.execute("DELETE FROM open_positions")
    connection.commit()
    connection.close()

    with pytest.raises(RuntimeError, match="LIVE open position not found"):
        service._record_live_stop_receipt(
            "US.MU", "ENTRY-1", "STOP-2", 98.0, 1
        )
