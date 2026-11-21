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
        raw_path = str(path)
        if raw_path.startswith("file:"):
            raw_path = raw_path[5:].split("?", 1)[0]
        if raw_path != ":memory:" and not Path(raw_path).resolve().is_relative_to(tmp_path):
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
    from app.core import position_sync as _position_sync
    monkeypatch.setattr(_position_sync, "_fetch_entry_vix_for_backfill", lambda: None)
    from app.playbook import market_regime
    from app.risk import data_health_guard  # Load real quote validator before risk stubs.

    def module(name, **attributes):
        replacement = ModuleType(name)
        replacement.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, name, replacement)

    push_hub = SimpleNamespace(push_text=MagicMock(), push=MagicMock())
    module("app.push", get_push_hub=lambda: push_hub,
           trade_entry=lambda **kwargs: kwargs, trade_exit=forbidden)
    config = {
        "playbook.execute_mode": "futu_simulate",
        "risk.playbook_vix_gate_enabled": False,
        "playbook.live_trial": False,
        # 2026-10-17: live_manual_arm 的 LIVE 执行前置要求两项 fail-mode 均 closed
        # (app/core/live_readiness.py, 审计 P1); real 参数化用例须显式提供该配置。
        "risk.acb_fail_mode": "closed",
        "risk.preflight_fail_mode": "closed",
        # RFC-130: real 参数化用例走 armed LIVE 前置 (arm 门默认 false 会 HALT_NEW_LIVE,
        # 补偿路径测的是"入场已提交后簿记失败"语义, 须在开火态下才可达)。
        "playbook.live_manual_arm": {"enabled": True, "max_open_positions": 1,
                                     "mode": "engineering_probe",
                                     "max_order_qty": 1, "session": "RTH",
                                     "probe": {"authorized": True,
                                               "authorization_id": "TEST-AUTH",
                                               "direction": "LONG"}},
    }
    _fake_cfg = SimpleNamespace(
        get_dotted=lambda key, default=None: config.get(key, default))
    module("app.core.config_container", get_config=lambda: _fake_cfg,
           # live_readiness 走 ConfigContainer.get_instance(); 替身须同接口,
           # 缺它时 LIVE 断言判 config_unreadable ⇒ fail-close 假红。
           ConfigContainer=SimpleNamespace(get_instance=lambda: _fake_cfg))
    module("app.risk.account_circuit_breaker", check_circuit_breaker=lambda equity, target_env="sim": SimpleNamespace(
        action="NORMAL", reason="sealed"))
    _capital_state = SimpleNamespace(current_equity=100000)
    _capital = SimpleNamespace(
        _state=_capital_state,
        can_open_position=lambda: (True, "sealed"),
        update_account=lambda **kwargs: _capital_state,
    )
    module("app.risk.capital_safety",
           get_capital_safety_system=lambda: _capital,
           get_capital_safety_system_for_arm=lambda target_env: _capital)
    module("app.risk.absolute_circuit_breaker", get_absolute_circuit_breaker=lambda target_env="sim": SimpleNamespace(
        can_open_new_position=lambda strategy: (True, "sealed"), get_position_scale=lambda strategy: 1))
    module("app.risk.pdt_guard", get_pdt_guard=lambda: SimpleNamespace(
        check_trade_permission=lambda: (True, "sealed", "NORMAL")))
    module("app.coordinator.manual_hold_registry", manual_hold_registry=SimpleNamespace(
        is_auto_blocked=lambda symbol, target_env=None: False, register=MagicMock()))
    module("app.playbook.admission", evaluate=lambda inst: {"verdict": "PASS"},
           apply=lambda inst: (inst, {"verdict": "PASS"}))
    monkeypatch.setattr(market_regime, "MarketRegimeGate", lambda: SimpleNamespace(
        assess=lambda: SimpleNamespace(regime="NORMAL", vix=20)))
    module("app.data.market_data_router", MarketDataRouter=MagicMock(), get_market_data_router=lambda: SimpleNamespace(
        get_full_market_context=lambda symbol: {"price": 100}, get_vix_or_none=lambda: 20))

    state = SimpleNamespace(failure="constructor", saved=[], violations=violations)
    broker = SimpleNamespace(
        get_account_info=MagicMock(return_value={
            "total_assets": 100000, "cash": 90000,
            "market_val": 10000, "buying_power": 180000,
        }),
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
            if state.failure == "save_trade":
                raise sqlite3.OperationalError("injected save_trade failure")
            with sqlite3.connect(state.trade_path) as trades:
                keys = ('source_type', 'symbol', 'direction', 'quantity', 'entry_price',
                        'futu_order_id', 'exit_price', 'exit_time')
                return trades.execute(
                    'INSERT INTO trade_records (' + ','.join(keys) + ') VALUES (?,?,?,?,?,?,?,?)',
                    tuple(record[k] for k in keys)).lastrowid

        upsert_position_simple = staticmethod(lambda **kwargs: None)

    module("app.storage.trade_store", TradeStore=FailingTradeStore)

    from app.playbook import playbook_service as service_module
    from app.playbook import pipeline
    from app.playbook.config import get_playbook_config
    from app.playbook.db import PlaybookDB
    from app.core import db_utils, paths
    from app.risk import sim_entry_budget

    monkeypatch.setattr(paths, "get_data_dir", lambda: tmp_path / "budget-data")

    # The isolated ledger has no account state; admission is a mocked boundary here.
    def budget_preflight(owner, **kwargs):
        return {"allowed": True, "quantity": kwargs["quantity"],
                "account_id": "SIM-1", "source": kwargs["source"],
                "symbol": kwargs["symbol"], "stop_loss": kwargs["stop_loss"],
                "reference_price": kwargs["entry_price"],
                "signed_qty": kwargs["quantity"] * (1 if kwargs["direction"] == "LONG" else -1)}

    def budget_finished(owner, **kwargs):
        sim_entry_budget._guard().clear_marker()
        return True

    monkeypatch.setattr(sim_entry_budget, "sim_entry_preflight", budget_preflight)
    monkeypatch.setattr(sim_entry_budget, "finish_sim_submission", budget_finished)

    monkeypatch.setattr(service_module, "now_et", lambda: datetime.fromisoformat("2026-09-10T10:00:00-04:00"))
    monkeypatch.setattr(service_module, "_SYMBOL_LOCK_DIR", tmp_path / "symbol-locks")
    monkeypatch.setattr(service_module, "_PORTFOLIO_GUARDRAIL_AUDIT_PATH", tmp_path / "portfolio.jsonl")
    from datetime import timezone
    import time
    monkeypatch.setattr(service_module, "_private_router_quote", lambda symbol: {
        "price": 100, "bid": 99.99, "ask": 100.01, "is_proxy": False,
        "update_time": datetime.now(timezone.utc).isoformat(), "timestamp": time.time(),
    })
    state.halt_state = {}
    def persist_halt(reason):
        state.halt_state = {"halted": True, "reason": reason}
        return dict(state.halt_state)
    halt = MagicMock(side_effect=persist_halt)
    monkeypatch.setattr("app.playbook.live_arm_gate.halt_live_new_positions", halt)
    monkeypatch.setattr("app.playbook.live_arm_gate.read_live_arm_halt", lambda: dict(state.halt_state))
    state.halt, state.push_hub = halt, push_hub
    monkeypatch.setattr("app.playbook.config.get_playbook_config", lambda: {
        "symbol_daily_loss_gate_enabled": False,
        "duplicate_position_guard_enabled": False,
        "pretrade_quote_gate_enabled": False,
        "entry_freshness_gate_enabled": False,
        "gap_dive_halve_enabled": False,
        "entry_confirm_max_wait_s": 0,
    })
    monkeypatch.setattr(pipeline, "_check_graduation_gate", lambda: {"graduated": True})
    monkeypatch.setattr(
        "app.core.scheduler_triggers.get_market_phase",
        lambda: SimpleNamespace(value="TRADING"),
    )
    book = MagicMock(return_value=True)
    monkeypatch.setattr(pipeline, "_book_playbook_exit_result", book)
    monkeypatch.setattr(db_utils, "is_live_mode", lambda: True)
    monkeypatch.setenv("AUTO_TRADE_ACCOUNT", "sim")
    monkeypatch.setenv("ALLOW_REAL_TRADING", "true")
    monkeypatch.setenv("REAL_ACCOUNT_ID", "123456789")

    trade_path = tmp_path / "source-trades.db"
    monkeypatch.setattr(paths, "get_trade_records_db", lambda: trade_path)
    monkeypatch.setattr(paths, "get_trade_records_db_for_arm", lambda target_env: trade_path)
    source = sqlite3.connect(trade_path)
    source.execute("CREATE TABLE voting_predictions (decision_id TEXT, model_votes TEXT, predicted_strength REAL, predicted_confidence REAL)")
    source.execute("CREATE TABLE trade_records (id INTEGER PRIMARY KEY, source_type TEXT, exit_time TEXT, "
                   "symbol TEXT, direction TEXT, quantity REAL, entry_price REAL, futu_order_id TEXT, exit_price REAL, "
                   "decision_id TEXT, entry_time TEXT)")
    source.commit()
    source.close()
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db = PlaybookDB(conn=conn)
    db.db_name = "ftseek_us_live"
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
    case.broker.submit_order.return_value["filled_qty"] = 1 if target_env == "real" else 10
    response = case.service.execute_instruction(
        case.iid, acknowledge_risk=True, target_env=target_env
    )
    assert response["success"] is False
    args, kwargs = case.broker.close_position.call_args
    expected_qty = 1 if target_env == "real" else 10
    assert args == ("US.COMP", expected_qty, "LONG")
    assert kwargs["target_env"] == target_env
    owner = kwargs["exit_owner"]
    assert owner == {
        "kind": "compensation", "db_path": str(case.trade_path.resolve()),
        "close_key": "entry:ENTRY-17", "instruction_id": case.iid, "execution_attempt": 1,
        "entry_order_id": "ENTRY-17", "entry_time": "2026-09-10T10:00:00-04:00",
        "entry_price": 100.25, "quantity": expected_qty, "symbol": "US.COMP", "direction": "LONG",
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
    case.broker.submit_order.return_value.update(filled_qty=4, broker_order_status="FILLED_PART")
    case.broker.cancel_order = MagicMock(return_value={"success": True})
    case.broker.query_order_status = MagicMock(return_value={
        "success": True, "order_id": "ENTRY-17", "code": "US.COMP", "qty": 10,
        "order_status": "CANCELLED_PART", "dealt_qty": 4, "dealt_avg_price": 100.25,
    })
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


def test_legacy_auto_real_path_fails_closed(compensation_case, monkeypatch):
    case = compensation_case
    monkeypatch.setenv("AUTO_TRADE_ACCOUNT", "real")

    response = case.service.execute_instruction(case.iid, acknowledge_risk=True)

    assert response["success"] is False
    assert "LIVE_PATH_A_RETIRED" in response["message"]
    case.broker.submit_order.assert_not_called()
    row = case.conn.execute(
        "SELECT execution_state, executed FROM playbook_instructions WHERE id=?",
        (case.iid,),
    ).fetchone()
    assert row["execution_state"] == "PENDING"
    assert not row["executed"]


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


def _entry_snapshot(status="CANCELLED_PART", qty=4, price=100.25, **extra):
    return {"success": True, "order_id": "ENTRY-17", "code": "US.COMP", "qty": 10,
            "order_status": status, "dealt_qty": qty, "dealt_avg_price": price, **extra}


def _partial_entry(case, snapshot):
    case.failure = None
    case.broker.submit_order.return_value.update(filled_qty=4, broker_order_status="FILLED_PART")
    case.broker.cancel_order = MagicMock(return_value={"success": True})
    case.broker.query_order_status = MagicMock(return_value=snapshot)
    return case.service.execute_instruction(case.iid, acknowledge_risk=True)


def _entry_row(case):
    return case.conn.execute("SELECT * FROM playbook_instructions WHERE id=?", (case.iid,)).fetchone()


@pytest.mark.parametrize("status,qty,price", [
    ("CANCELLED_PART", 4, 100.25), ("FILLED_ALL", 10, 101.75),
])
def test_entry_terminal_receipt_replaces_submit_partial(compensation_case, status, qty, price):
    case = compensation_case
    response = _partial_entry(case, _entry_snapshot(status, qty, price))
    assert response["success"] is True
    assert case.saved[0]["quantity"] == qty
    assert case.saved[0]["entry_price"] == price
    assert _entry_row(case)["execution_state"] == "BOOKED"
    case.broker.cancel_order.assert_called_once_with("ENTRY-17", "sim")
    case.broker.query_order_status.assert_called_once_with("ENTRY-17", "sim")
    case.broker.close_position.assert_not_called()
    assert not case.service.execute_instruction(case.iid, acknowledge_risk=True)["success"]
    assert len(case.saved) == 1


@pytest.mark.parametrize("snapshot", [
    _entry_snapshot("FILLED_PART"),  # successful cancel ACK, still in flight
    {"success": False}, {},
    _entry_snapshot("CANCELLED_ALL", 0),
    _entry_snapshot("CANCELLED_PART", 2),  # regressive cumulative quantity
    _entry_snapshot("CANCELLED_PART", 4, 0),
    _entry_snapshot("FILLED_ALL", 10, None),
    _entry_snapshot("FILLED_ALL", 11),
    _entry_snapshot("CANCELLED_PART", 4.5),
    _entry_snapshot("CANCELLED_PART", float("nan")),
    _entry_snapshot("CANCELLED_PART", 4, float("inf")),
    _entry_snapshot("FILLED_ALL", None),
    _entry_snapshot(order_id="WRONG"),
    _entry_snapshot(code="US.OTHER"),
])
def test_unresolved_partial_never_books_or_releases(compensation_case, snapshot):
    import json
    case = compensation_case
    assert _partial_entry(case, snapshot)["success"] is False
    row = _entry_row(case)
    evidence = json.loads(row["metadata_json"])["entry_fill"]
    assert row["execution_state"] == "FILLED_UNBOOKED"
    assert evidence["observed_qty"] >= 4  # shares, not notional or requested quantity
    assert not case.db.unlock_execution_if_zero_fill_terminal(case.iid, 1, "CANCELLED_ALL", 0)
    assert case.db.try_claim_execution(case.iid) is None
    assert case.saved == []
    case.broker.close_position.assert_not_called()


def test_mom_null_thesis_books_without_compensation(compensation_case):
    """MOM 写入方省略 thesis 时，真实服务层必须完成首单簿记而非补偿平仓。"""
    import json
    case = compensation_case
    case.failure = None
    case.conn.execute(
        "UPDATE playbook_instructions SET thesis=NULL, decision_id=?, position_size=49 WHERE id=?",
        ("MOM-ETF-V1-20260910-US.COMP", case.iid),
    )
    case.conn.commit()
    case.broker.submit_order.return_value["filled_qty"] = 49

    response = case.service.execute_instruction(case.iid, acknowledge_risk=True)

    assert response["success"] is True, response
    assert _entry_row(case)["execution_state"] == "BOOKED"
    assert len(case.saved) == 1
    metadata = json.loads(case.saved[0]["metadata_json"])
    assert metadata["thesis"] == ""
    case.broker.close_position.assert_not_called()


def test_query_exception_keeps_positive_evidence(compensation_case):
    import json
    case = compensation_case
    case.broker.query_order_status = MagicMock(side_effect=TimeoutError("sealed timeout"))
    case.broker.cancel_order = MagicMock(return_value={"success": False})
    case.broker.submit_order.return_value.update(filled_qty=4, broker_order_status="FILLED_PART")
    assert not case.service.execute_instruction(case.iid, acknowledge_risk=True)["success"]
    assert json.loads(_entry_row(case)["metadata_json"])["entry_fill"]["observed_qty"] == 4
    assert _entry_row(case)["execution_state"] == "FILLED_UNBOOKED"


def test_same_symbol_new_instruction_blocked_until_zero_terminal(compensation_case):
    case = compensation_case
    assert not _partial_entry(case, _entry_snapshot("FILLED_PART"))["success"]
    other = case.db.save_instruction({
        "date": "2026-09-11", "symbol": "US.COMP", "direction": "BUY",
        "entry_price": 100, "stop_loss": 95, "position_size": 10,
    })
    assert other != case.iid
    assert case.db.try_claim_execution(other) is None


def test_order_observation_cas_and_metadata_units(compensation_case):
    import json
    case = compensation_case
    attempt = case.db.try_claim_execution(case.iid)
    assert case.db.observe_execution_order(case.iid, attempt + 1, "ENTRY-17", "sim", _entry_snapshot()) is None
    first = case.db.observe_execution_order(
        case.iid, attempt, "ENTRY-17", "sim", _entry_snapshot("FILLED_PART", 4, 100.25))
    assert first["requested_qty"] == 10  # shares
    assert first["observed_qty"] == 4  # cumulative shares
    assert first["price"] == 100.25  # USD/share, not 401 USD notional
    assert first["confirmed"] is False
    assert not case.db.mark_execution_booked(case.iid, attempt, 100.25)
    assert case.db.observe_execution_order(case.iid, attempt, "OTHER", "sim", _entry_snapshot()) is None
    case.db.observe_execution_order(case.iid, attempt, "ENTRY-17", "sim", _entry_snapshot("CANCELLED_ALL", 0))
    assert json.loads(_entry_row(case)["metadata_json"])["entry_fill"]["observed_qty"] == 4
    assert not case.db.unlock_execution_if_zero_fill_terminal(case.iid, attempt, "CANCELLED_ALL", 0)
    case.db.observe_execution_order(case.iid, attempt, "ENTRY-17", "sim", _entry_snapshot("FILLED_ALL", 10, 101.75))
    assert case.db.mark_execution_booked(case.iid, attempt, 101.75)
    assert not case.db.mark_execution_booked(case.iid, attempt, 101.75)
    assert case.db.observe_execution_order(case.iid, attempt, "ENTRY-17", "sim", _entry_snapshot()) is None


@pytest.fixture
def entry_recovery_case(compensation_case, monkeypatch):
    from app.core import position_sync
    from app.playbook.db import PlaybookDB
    case = compensation_case
    path = case.trade_path.with_name("recovery.sqlite")
    with sqlite3.connect(case.trade_path) as trades:
        schema = trades.execute("SELECT sql FROM sqlite_master WHERE name='trade_records'").fetchone()[0]
    conn = sqlite3.connect(path)
    case.conn.backup(conn)
    conn.execute(schema)
    conn.commit()
    conn.row_factory = sqlite3.Row
    case.conn, case.db, case.trade_path = conn, PlaybookDB(conn=conn), path
    case.service.db = case.db
    case.failure = None
    monkeypatch.setattr(position_sync, "_get_db_path", lambda: str(path))
    case.sync = position_sync.USPositionSync.__new__(position_sync.USPositionSync)
    case.ctx = MagicMock()
    yield case
    conn.close()


def _reconcile(case, snapshot, position=None):
    import pandas as pd
    case.ctx.order_list_query.return_value = (0, pd.DataFrame([snapshot]))
    # Deliberately unrelated aggregate: must never become this order's quantity/cost.
    case.sync._reconcile_playbook_execution_states(
        case.ctx, "SIMULATE", "sim", {"US.COMP": position if position is not None else {
            "qty": 900, "cost": 9, "direction": "LONG"}})


def test_recovery_rechecks_unbooked_and_uses_only_order_receipt(entry_recovery_case):
    case = entry_recovery_case
    attempt = case.db.try_claim_execution(case.iid)
    case.db.mark_execution_submitted(case.iid, attempt, "ENTRY-17", "sim")
    _reconcile(case, _entry_snapshot("FILLED_PART"))
    assert _entry_row(case)["execution_state"] == "FILLED_UNBOOKED"
    assert case.saved == []
    _reconcile(case, _entry_snapshot("CANCELLED_ALL", 0))
    assert _entry_row(case)["execution_state"] == "FILLED_UNBOOKED"
    _reconcile(case, _entry_snapshot("CANCELLED_PART", 4, 100.25))
    assert _entry_row(case)["execution_state"] == "BOOKED"
    assert [(r["quantity"], r["entry_price"], r["direction"]) for r in case.saved] == [(4, 100.25, "LONG")]
    _reconcile(case, _entry_snapshot("FILLED_ALL", 10, 105))
    assert len(case.saved) == 1  # existing BOOKED history is untouched
    assert all(call.kwargs["refresh_cache"] is True for call in case.ctx.order_list_query.call_args_list)


@pytest.mark.parametrize("qty,price,order_id,books", [
    (4, 100.25, "ENTRY-17", True), (3, 100.25, "ENTRY-17", False),
    (4, 100.5, "ENTRY-17", False), (4, 100.25, "OLD", False),
])
def test_recovery_existing_trade_requires_same_identity_and_receipt(entry_recovery_case, qty, price, order_id, books):
    case = entry_recovery_case
    attempt = case.db.try_claim_execution(case.iid)
    case.db.mark_execution_submitted(case.iid, attempt, "ENTRY-17", "sim")
    case.conn.execute("INSERT INTO trade_records(source_type,symbol,direction,quantity,entry_price,futu_order_id,exit_price,exit_time) "
                      "VALUES('PLAYBOOK_SIM','US.COMP','LONG',?,?,?,0,'')", (qty, price, order_id))
    case.conn.commit()
    _reconcile(case, _entry_snapshot())
    assert (_entry_row(case)["execution_state"] == "BOOKED") is books
    assert case.saved == []
    assert case.conn.execute("SELECT COUNT(*) FROM trade_records").fetchone()[0] == 1


def test_recovery_cannot_overlap_service_symbol_critical_section(entry_recovery_case):
    case = entry_recovery_case
    def cancel(*args):
        _reconcile(case, _entry_snapshot())
        case.ctx.order_list_query.assert_not_called()
        return {"success": True}
    case.broker.submit_order.return_value.update(filled_qty=4, broker_order_status="FILLED_PART")
    case.broker.cancel_order = MagicMock(side_effect=cancel)
    case.broker.query_order_status = MagicMock(return_value=_entry_snapshot())
    assert case.service.execute_instruction(case.iid, acknowledge_risk=True)["success"]
    _reconcile(case, _entry_snapshot())
    assert len(case.saved) == 1


def test_unresolved_real_fill_halts_and_alerts_without_claiming_stop(compensation_case, monkeypatch):
    case = compensation_case
    monkeypatch.setenv("AUTO_TRADE_ACCOUNT", "real")
    case.broker.submit_order.return_value.update(filled_qty=1, broker_order_status="FILLED_PART")
    case.broker.cancel_order = MagicMock(return_value={"success": True})
    snapshot = _entry_snapshot("FILLED_PART", 1)
    snapshot["qty"] = 1
    case.broker.query_order_status = MagicMock(return_value=snapshot)
    case.broker.place_protective_stop = MagicMock()
    response = case.service.execute_instruction(case.iid, acknowledge_risk=True, target_env="real")
    assert response["success"] is False
    case.halt.assert_called_once()
    assert "HALT_NEW_LIVE" in case.push_hub.push_text.call_args.args[0]
    case.broker.place_protective_stop.assert_not_called()
    assert _entry_row(case)["execution_state"] == "FILLED_UNBOOKED"


def test_verified_real_full_fill_preserves_protective_stop_path(compensation_case, monkeypatch):
    case = compensation_case
    case.failure = None
    monkeypatch.setenv("AUTO_TRADE_ACCOUNT", "real")
    case.broker.submit_order.return_value["filled_qty"] = 1
    case.broker.place_protective_stop = MagicMock(return_value={"success": True, "futu_order_id": "STOP-17"})
    receipt = MagicMock()
    monkeypatch.setattr(case.service, "_record_live_stop_receipt", receipt)
    response = case.service.execute_instruction(case.iid, acknowledge_risk=True, target_env="real")
    assert response["success"] is True
    case.broker.place_protective_stop.assert_called_once_with("US.COMP", 1, "LONG", 95.0, target_env="real")
    receipt.assert_called_once()
    case.halt.assert_not_called()


@pytest.mark.parametrize('position', [{}, {'qty': 2, 'direction': 'LONG'}, {'qty': 10, 'direction': 'SHORT'}])
def test_recovery_does_not_invent_open_trade_after_broker_close(entry_recovery_case, position):
    case = entry_recovery_case
    attempt = case.db.try_claim_execution(case.iid)
    case.db.mark_execution_submitted(case.iid, attempt, 'ENTRY-17', 'sim')
    _reconcile(case, _entry_snapshot(), position=position)
    assert _entry_row(case)['execution_state'] == 'FILLED_UNBOOKED'
    assert case.saved == []


def test_p3_legacy_recovery_queries_order_instead_of_copying_account_position(entry_recovery_case):
    case = entry_recovery_case
    case.broker.query_order_status = MagicMock(return_value=_entry_snapshot())
    assert case.sync._backfill_playbook_trade_record(
        'US.COMP', 'LONG', 900, 9, 95, 110, 'legacy', 'ENTRY-17', 'sim')
    assert case.saved[0]['quantity'] == 4
    assert case.saved[0]['entry_price'] == 100.25
    case.broker.query_order_status.assert_called_once_with('ENTRY-17', 'sim')


def test_p3_legacy_numeric_broker_order_id_matches_string_identity(entry_recovery_case):
    from numpy import int64
    case = entry_recovery_case
    case.broker.query_order_status = MagicMock(return_value=_entry_snapshot(order_id=int64(123)))
    for _ in range(2):
        assert case.sync._backfill_playbook_trade_record(
            'US.COMP', 'LONG', 900, 9, 95, 110, 'legacy-numeric', '123', 'sim')
    assert len(case.saved) == 1
    assert case.saved[0]['futu_order_id'] == '123'
    assert case.saved[0]['quantity'] == 4
    assert case.saved[0]['entry_price'] == 100.25


def test_p3_unresolved_instruction_defers_to_b1(entry_recovery_case):
    case = entry_recovery_case
    case.db.try_claim_execution(case.iid)
    case.broker.query_order_status = MagicMock(return_value=_entry_snapshot())
    assert not case.sync._backfill_playbook_trade_record(
        'US.COMP', 'LONG', 900, 9, 95, 110, 'legacy', 'ENTRY-17', 'sim')
    case.broker.query_order_status.assert_not_called()
    assert case.saved == []


def test_dotted_class_share_keeps_exact_broker_identity(compensation_case):
    case = compensation_case
    case.conn.execute("UPDATE playbook_instructions SET symbol='US.BRK.B' WHERE id=?", (case.iid,))
    case.conn.commit()
    attempt = case.db.try_claim_execution(case.iid)
    snapshot = _entry_snapshot(code='US.BRK.B')
    assert case.db.observe_execution_order(case.iid, attempt, 'ENTRY-17', 'sim', snapshot)['confirmed']
    assert case.db.observe_execution_order(case.iid, attempt, 'ENTRY-17', 'sim',
                                           _entry_snapshot(code='US.BRK-B')) is None


def test_observation_metadata_cas_remerges_concurrent_annotation(compensation_case, monkeypatch):
    import json
    case = compensation_case
    attempt = case.db.try_claim_execution(case.iid)
    raced = []
    class RacingConnection:
        def execute(self, sql, args=()):
            if 'SET execution_state=?, broker_order_id=?' in sql and not raced:
                raced.append(True)
                case.conn.execute("UPDATE playbook_instructions SET metadata_json=? WHERE id=?",
                                  (json.dumps({'other_writer': 'keep'}), case.iid))
                case.conn.commit()
            return case.conn.execute(sql, args)
        def commit(self):
            case.conn.commit()
    monkeypatch.setattr(case.db, '_conn', lambda: RacingConnection())
    observation = case.db.observe_execution_order(case.iid, attempt, 'ENTRY-17', 'sim', _entry_snapshot())
    assert observation['confirmed']
    meta = json.loads(_entry_row(case)['metadata_json'])
    assert meta['other_writer'] == 'keep'
    assert meta['entry_fill']['observed_qty'] == 4


@pytest.mark.parametrize('qty', [None, '', float('nan'), -1])
def test_missing_or_invalid_zero_evidence_does_not_unlock(compensation_case, qty):
    case = compensation_case
    attempt = case.db.try_claim_execution(case.iid)
    assert not case.db.unlock_execution_if_zero_fill_terminal(case.iid, attempt, 'CANCELLED_ALL', qty)
    assert _entry_row(case)['execution_state'] == 'UNKNOWN'


@pytest.mark.parametrize('submit_state', ['UNKNOWN', 'DEFINITIVE_REJECT'])
def test_uncertain_submit_without_id_retains_positive_quantity(compensation_case, submit_state):
    import json
    case = compensation_case
    case.broker.submit_order.return_value.update(
        submit_state=submit_state, futu_order_id='', filled_qty=4, broker_order_status='FILLED_PART')
    assert not case.service.execute_instruction(case.iid, acknowledge_risk=True)['success']
    evidence = json.loads(_entry_row(case)['metadata_json'])['entry_fill']
    assert evidence['observed_qty'] == 4 and not evidence['confirmed']
    assert _entry_row(case)['execution_state'] == 'FILLED_UNBOOKED'
    assert not case.db.unlock_execution_if_zero_fill_terminal(case.iid, 1, 'CANCELLED_ALL', 0)
    discovered = case.db.observe_execution_order(case.iid, 1, 'ENTRY-17', 'sim', _entry_snapshot(qty=2))
    assert discovered['observed_qty'] == 4 and not discovered['confirmed']


def test_unresolved_live_alert_only_on_halt_transition_or_reason_change(compensation_case):
    case = compensation_case
    notify = case.service._halt_unresolved_live_entry
    notify('real', 'US.COMP', 'ENTRY-17', 'unconfirmed')
    notify('real', 'US.COMP', 'ENTRY-17', 'unconfirmed')
    assert case.halt.call_count == 1
    assert case.push_hub.push_text.call_count == 1
    notify('real', 'US.COMP', 'ENTRY-17', 'query_failed')
    assert case.halt.call_count == 2
    assert case.push_hub.push_text.call_count == 2
    case.halt_state['halted'] = False  # Simulated manual clear, not a production mutation.
    notify('real', 'US.COMP', 'ENTRY-17', 'query_failed')
    assert case.halt.call_count == 3
    assert case.push_hub.push_text.call_count == 3


def test_dual_arm_mark_propagates_to_booking_metadata(compensation_case):
    """C13/C14 后半 (DEB-20261116): 镜像指令的 dual_arm 标记必须传播到
    trade_records 簿记层 metadata —— 真钱成交事后可按臂归因。"""
    import json
    case = compensation_case
    case.failure = None
    mark = {"target_env": "real", "source_instruction_id": 99,
            "decision_id": "DA-DUAL", "mode": "strategy_probe", "s1_arm_enabled": True}
    case.conn.execute(
        "UPDATE playbook_instructions SET metadata_json=?, position_size=49 WHERE id=?",
        (json.dumps({"dual_arm": mark}), case.iid),
    )
    case.conn.commit()
    case.broker.submit_order.return_value["filled_qty"] = 49

    response = case.service.execute_instruction(case.iid, acknowledge_risk=True)

    assert response["success"] is True, response
    assert len(case.saved) == 1
    metadata = json.loads(case.saved[0]["metadata_json"])
    assert metadata["dual_arm"] == mark


def test_sim_booking_without_dual_arm_mark_is_null(compensation_case):
    """sim 指令无镜像标记 ⇒ dual_arm=None, 不得伪造归因。"""
    import json
    case = compensation_case
    case.failure = None
    case.conn.execute(
        "UPDATE playbook_instructions SET position_size=49 WHERE id=?", (case.iid,))
    case.conn.commit()
    case.broker.submit_order.return_value["filled_qty"] = 49

    response = case.service.execute_instruction(case.iid, acknowledge_risk=True)

    assert response["success"] is True, response
    metadata = json.loads(case.saved[0]["metadata_json"])
    assert metadata["dual_arm"] is None
