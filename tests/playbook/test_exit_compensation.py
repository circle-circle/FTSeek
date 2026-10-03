"""Real PlaybookService entry-bookkeeping compensation and M2 lock contracts.

All application imports occur after fixture I/O guards. Broker, risk/quote, failing
TradeStore and exit-booking boundaries are faked; service dispatch and PlaybookDB
claim/submitted/filled-unbooked transitions run unchanged on real SQLite.
"""
import socket
import sqlite3
import sqlite3.dbapi2
import sys
from datetime import datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def compensation_case(monkeypatch, tmp_path):
    violations = []
    original_connect = sqlite3.connect

    def forbidden(*args, **kwargs):
        violations.append((args, kwargs))
        raise AssertionError("external compensation-test boundary attempted")

    def safe_connect(path, *args, **kwargs):
        if str(path) != ":memory:" and not Path(path).resolve().is_relative_to(tmp_path):
            return forbidden(path, *args, **kwargs)
        return original_connect(path, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", safe_connect)
    monkeypatch.setattr(sqlite3.dbapi2, "connect", safe_connect)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket.socket, "sendto", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    import futu
    monkeypatch.setattr(futu, "OpenSecTradeContext", forbidden)
    monkeypatch.setattr(futu, "OpenQuoteContext", forbidden)
    from app.playbook import market_regime

    def module(name, **attributes):
        replacement = ModuleType(name)
        replacement.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, name, replacement)

    module("app.push", get_push_hub=forbidden, trade_entry=forbidden, trade_exit=forbidden)
    config = {
        "playbook.execute_mode": "futu_simulate",
        "risk.playbook_vix_gate_enabled": False,
        "playbook.live_trial": False,
        # RFC-130: real 参数化用例走 armed LIVE 前置 (arm 门默认 false 会 HALT_NEW_LIVE,
        # 补偿路径测的是"入场已提交后簿记失败"语义, 须在开火态下才可达)。
        "playbook.live_manual_arm": {"enabled": True, "max_open_positions": 1,
                                     "max_order_qty": 1, "session": "RTH"},
    }
    module("app.core.config_container", get_config=lambda: SimpleNamespace(
        get_dotted=lambda key, default=None: config.get(key, default)))
    module("app.risk.account_circuit_breaker", check_circuit_breaker=lambda equity: SimpleNamespace(
        action="NORMAL", reason="sealed"))
    module("app.risk.capital_safety", get_capital_safety_system=lambda: SimpleNamespace(
        _state=SimpleNamespace(current_equity=100000), can_open_position=lambda: (True, "sealed")))
    module("app.risk.absolute_circuit_breaker", get_absolute_circuit_breaker=lambda: SimpleNamespace(
        can_open_new_position=lambda strategy: (True, "sealed"), get_position_scale=lambda strategy: 1))
    module("app.risk.pdt_guard", get_pdt_guard=lambda: SimpleNamespace(
        check_trade_permission=lambda: (True, "sealed", "NORMAL")))
    module("app.coordinator.manual_hold_registry", manual_hold_registry=SimpleNamespace(
        is_auto_blocked=lambda symbol: False, register=forbidden))
    module("app.playbook.admission", evaluate=lambda inst: {"verdict": "PASS"},
           apply=lambda inst: (inst, {"verdict": "PASS"}))
    monkeypatch.setattr(market_regime, "MarketRegimeGate", lambda: SimpleNamespace(
        assess=lambda: SimpleNamespace(regime="NORMAL", vix=20)))
    module("app.data.market_data_router", get_market_data_router=lambda: SimpleNamespace(
        get_full_market_context=lambda symbol: {"price": 100}, get_vix_or_none=lambda: 20))

    state = SimpleNamespace(failure="constructor", saved=[], violations=violations)
    broker = SimpleNamespace(
        submit_order=MagicMock(return_value={
            "success": True, "submit_state": "ACCEPTED", "futu_order_id": "ENTRY-17",
            "broker_order_status": "FILLED_ALL", "filled_price": 100.25, "filled_qty": 10,
        }),
        close_position=MagicMock(return_value={
            "success": False, "order_filled": False, "dealt_qty": 0,
            "exit_intent_id": "compensation-intent", "exit_state": "PENDING",
        }),
    )
    module("app.execution.dual_broker", get_dual_broker=lambda: broker)

    class FailingTradeStore:
        def __init__(self, path):
            state.constructor_path = path
            if state.failure == "constructor":
                raise sqlite3.OperationalError("injected TradeStore constructor failure")

        def save_trade(self, record, **kwargs):
            state.saved.append(record)
            raise sqlite3.OperationalError("injected save_trade failure")

        upsert_position_simple = staticmethod(forbidden)

    module("app.storage.trade_store", TradeStore=FailingTradeStore)

    from app.playbook import playbook_service as service_module
    from app.playbook import pipeline
    from app.playbook.config import get_playbook_config
    from app.playbook.db import PlaybookDB
    from app.core import db_utils, paths

    monkeypatch.setattr(service_module, "now_et", lambda: datetime.fromisoformat("2026-09-10T10:00:00-04:00"))
    monkeypatch.setattr(service_module, "_private_router_quote", lambda symbol: {"price": 100})
    monkeypatch.setattr("app.playbook.config.get_playbook_config", lambda: {
        "symbol_daily_loss_gate_enabled": False,
        "duplicate_position_guard_enabled": False,
        "pretrade_quote_gate_enabled": False,
        "entry_freshness_gate_enabled": False,
        "gap_dive_halve_enabled": False,
    })
    monkeypatch.setattr(pipeline, "_check_graduation_gate", lambda: {"graduated": True})
    book = MagicMock(return_value=True)
    monkeypatch.setattr(pipeline, "_book_playbook_exit_result", book)
    monkeypatch.setattr(db_utils, "is_live_mode", lambda: True)
    monkeypatch.setenv("AUTO_TRADE_ACCOUNT", "sim")
    monkeypatch.setenv("ALLOW_REAL_TRADING", "true")

    trade_path = tmp_path / "source-trades.db"
    monkeypatch.setattr(paths, "get_trade_records_db", lambda: trade_path)
    source = sqlite3.connect(trade_path)
    source.execute("CREATE TABLE voting_predictions (decision_id TEXT, model_votes TEXT, predicted_strength REAL, predicted_confidence REAL)")
    source.commit()
    source.close()
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db = PlaybookDB(conn=conn)
    service = service_module.PlaybookService(playbook_db=db)
    iid = db.save_instruction({
        "date": "2026-09-10", "symbol": "US.COMP", "direction": "BUY",
        "entry_price": 100, "stop_loss": 95, "take_profit": 110, "position_size": 10,
        "confidence": 70, "decision_id": "DA-20260910-COMP", "thesis": "sealed fixture",
    })
    unlock = MagicMock(wraps=db.unlock_execution_if_zero_fill_terminal)
    monkeypatch.setattr(db, "unlock_execution_if_zero_fill_terminal", unlock)
    state.service, state.db, state.conn, state.iid = service, db, conn, iid
    state.broker, state.book, state.unlock, state.trade_path = broker, book, unlock, trade_path
    try:
        yield state
    finally:
        conn.close()
        assert violations == [], f"swallowed boundary attempts: {violations}"


def assert_m2_locked(case):
    row = case.conn.execute("SELECT * FROM playbook_instructions WHERE id=?", (case.iid,)).fetchone()
    assert row["execution_state"] == "FILLED_UNBOOKED"
    assert row["broker_order_status"] == "BOOKKEEPING_FAILED"
    assert row["broker_order_id"] == "ENTRY-17"
    assert row["execution_attempt"] == 1
    assert row["executed"] == 1
    case.unlock.assert_not_called()
    assert case.db.try_claim_execution(case.iid) is None
    first_close_count = case.broker.close_position.call_count
    again = case.service.execute_instruction(case.iid, acknowledge_risk=True)
    assert again["success"] is False
    case.broker.submit_order.assert_called_once()
    assert case.broker.close_position.call_count == first_close_count


@pytest.mark.parametrize("failure", ["constructor", "save_trade"])
@pytest.mark.parametrize("evidence,should_book", [
    ({"success": False, "order_filled": False, "dealt_qty": 0}, False),
    ({"success": True, "order_filled": False, "dealt_qty": 10}, False),
    ({"success": True, "order_filled": True, "dealt_qty": 4}, False),
    ({"success": True, "order_filled": True, "dealt_qty": None}, False),
    ({"success": True, "order_filled": True, "dealt_qty": 11}, False),
    ({"success": False, "order_filled": True, "dealt_qty": 10}, False),
    ({"success": True, "order_filled": True, "dealt_qty": 10}, True),
])
def test_real_service_compensation_evidence_gate(compensation_case, failure, evidence, should_book):
    case = compensation_case
    case.failure = failure
    result = {"exit_intent_id": "compensation-intent", "filled_price": 99, **evidence}
    case.broker.close_position.return_value = result
    response = case.service.execute_instruction(case.iid, acknowledge_risk=True)
    assert response["success"] is False
    assert "injected" in response["message"]
    case.broker.close_position.assert_called_once()
    if should_book:
        case.book.assert_called_once_with(case.broker, result)
    else:
        case.book.assert_not_called()
    assert len(case.saved) == (failure == "save_trade")
    assert case.constructor_path == str(case.trade_path.resolve())
    assert_m2_locked(case)


@pytest.mark.parametrize("target_env", ["sim", "real"])
@pytest.mark.parametrize("failure", ["constructor", "save_trade"])
def test_compensation_carries_original_source_and_entry_identity(compensation_case, monkeypatch, target_env, failure):
    case = compensation_case
    case.failure = failure
    monkeypatch.setenv("AUTO_TRADE_ACCOUNT", target_env)
    response = case.service.execute_instruction(case.iid, acknowledge_risk=True)
    assert response["success"] is False
    args, kwargs = case.broker.close_position.call_args
    assert args == ("US.COMP", 10, "LONG")
    assert kwargs["target_env"] == target_env
    owner = kwargs["exit_owner"]
    assert owner == {
        "kind": "compensation", "db_path": str(case.trade_path.resolve()),
        "close_key": "entry:ENTRY-17", "instruction_id": case.iid, "execution_attempt": 1,
        "entry_order_id": "ENTRY-17", "entry_time": "2026-09-10T10:00:00-04:00",
        "entry_price": 100.25, "quantity": 10, "symbol": "US.COMP", "direction": "LONG",
        "source_type": "PLAYBOOK_LIVE" if target_env == "real" else "PLAYBOOK_SIM",
    }
    assert kwargs["exit_decision"] == {
        "reason": "ENTRY_BOOKKEEPING_COMPENSATION", "reference_price": 100.25,
        "metadata": {"playbook_instruction_id": case.iid}, "holding_hours": 0.0,
    }
    if case.saved:
        original = case.saved[0]
        for owner_key, trade_key in (("entry_order_id", "futu_order_id"), ("entry_time", "entry_time"),
                                     ("quantity", "quantity"), ("source_type", "source_type")):
            assert owner[owner_key] == original[trade_key]
    assert_m2_locked(case)


def test_partial_entry_compensates_only_observed_owned_quantity(compensation_case):
    case = compensation_case
    case.broker.submit_order.return_value["filled_qty"] = 4
    case.broker.close_position.return_value = {
        "success": True, "order_filled": True, "dealt_qty": 4,
        "exit_intent_id": "compensation-partial-entry", "filled_price": 99,
    }
    response = case.service.execute_instruction(case.iid, acknowledge_risk=True)
    assert response["success"] is False
    args, kwargs = case.broker.close_position.call_args
    assert args == ("US.COMP", 4, "LONG")
    assert kwargs["exit_owner"]["quantity"] == 4
    case.book.assert_called_once_with(case.broker, case.broker.close_position.return_value)
    assert_m2_locked(case)


@pytest.mark.parametrize("failing_boundary", ["close", "book"])
def test_compensation_boundary_failure_never_releases_entry(compensation_case, failing_boundary):
    case = compensation_case
    if failing_boundary == "close":
        case.broker.close_position.side_effect = RuntimeError("exit ledger unavailable")
    else:
        case.broker.close_position.return_value = {
            "success": True, "order_filled": True, "dealt_qty": 10,
            "exit_intent_id": "compensation-unbooked", "filled_price": 99,
        }
        case.book.side_effect = sqlite3.OperationalError("exit receipt unavailable")
    response = case.service.execute_instruction(case.iid, acknowledge_risk=True)
    assert response["success"] is False
    case.broker.close_position.assert_called_once()
    assert case.book.call_count == (failing_boundary == "book")
    assert_m2_locked(case)
