"""C2: only live Playbook entries fail closed on missing or unhealthy market data."""

import inspect
import json
import socket
import sqlite3
import sqlite3.dbapi2
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.playbook.db import PlaybookDB
from app.playbook.playbook_service import PlaybookService


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    playbook_db = PlaybookDB(conn=conn)
    playbook_db.ensure_tables()
    yield playbook_db
    conn.close()


@pytest.fixture
def sealed_io(monkeypatch, tmp_path):
    violations = []
    connect = sqlite3.connect

    def forbidden(*args, **kwargs):
        violations.append((args, kwargs))
        raise AssertionError("quote test attempted external I/O")

    def sandbox_connect(path, *args, **kwargs):
        raw = str(path).removeprefix("file:").split("?", 1)[0]
        if raw != ":memory:" and not Path(raw).resolve().is_relative_to(tmp_path):
            return forbidden(path, *args, **kwargs)
        return connect(path, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", sandbox_connect)
    monkeypatch.setattr(sqlite3.dbapi2, "connect", sandbox_connect)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    import futu
    monkeypatch.setattr(futu, "OpenQuoteContext", forbidden)
    monkeypatch.setattr(futu, "OpenSecTradeContext", forbidden)
    yield
    assert not violations


@pytest.fixture
def svc(db, monkeypatch, tmp_path, sealed_io):
    monkeypatch.setattr(
        "app.risk.pdt_guard.get_pdt_guard",
        lambda: SimpleNamespace(check_trade_permission=lambda: (True, "test", "NORMAL")),
    )
    monkeypatch.setattr(
        "app.coordinator.manual_hold_registry.manual_hold_registry.is_auto_blocked",
        lambda symbol, target_env=None: False,
    )
    monkeypatch.setattr(
        "app.risk.symbol_day_loss.is_symbol_day_loss_exceeded",
        lambda symbol: (False, 0.0),
    )
    main_db = tmp_path / "quote-gate-positions.sqlite"
    conn = sqlite3.connect(main_db)
    try:
        conn.execute("CREATE TABLE open_positions (symbol TEXT, size REAL, owner_strategy TEXT)")
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr("app.core.db_utils.get_main_db", lambda: main_db)
    db.db_name = "ftseek_us_live"
    live_ledger = tmp_path / "live-ledger.sqlite"
    monkeypatch.setattr(
        "app.core.paths.get_trade_records_db_for_arm",
        lambda target_env: live_ledger,
    )
    monkeypatch.setattr("app.push.get_push_hub", lambda: MagicMock())
    return PlaybookService(playbook_db=db)


def _save_inst(db, symbol="US.TEST_C2"):
    return db.save_instruction({
        "date": "2026-09-10",
        "symbol": symbol,
        "direction": "BUY",
        "entry_price": 100.0,
        "stop_loss": 95.0,
        "take_profit": 110.0,
        "position_size": 40,
        "confidence": 70,
        "conditions": ["pullback_to_confirmation"],
        "raw_instruction_json": json.dumps({"source": "test"}),
    })


def _mk_dual():
    dual = MagicMock()
    dual._ctx_and_acc.return_value = (MagicMock(), "12345", "sim", threading.Lock())
    captured = {}

    def submit(order, target_env="sim"):
        captured["called"] = True
        captured["target_env"] = target_env
        captured["order_qty"] = getattr(order, "quantity", None)
        return {
            "success": False,
            "submit_state": "DEFINITIVE_REJECT",
            "futu_order_id": "",
            "error": "test reject",
        }

    dual.submit_order.side_effect = submit
    return dual, captured


def _call_inner(svc, iid, target_env):
    inst = svc._find_instruction_by_id(iid)
    return svc._execute_via_dual_broker_inner(
        inst,
        iid,
        inst["symbol"],
        inst["direction"],
        float(inst["entry_price"]),
        float(inst["stop_loss"]),
        float(inst["take_profit"]),
        int(inst["position_size"]),
        "LONG",
        "",
        target_env,
    )


def _live_prereqs(monkeypatch, db_live=True, armed=True):
    monkeypatch.setenv("ALLOW_REAL_TRADING", "true")
    monkeypatch.setenv("REAL_ACCOUNT_ID", "123456789")
    monkeypatch.setattr("app.core.db_utils.is_live_mode", lambda: db_live)
    monkeypatch.setattr(
        "app.playbook.pipeline._check_graduation_gate",
        lambda: {"graduated": True},
    )
    monkeypatch.setattr(
        "app.core.scheduler_triggers.get_market_phase",
        lambda: SimpleNamespace(value="TRADING"),
    )
    # RFC-130 (2026-10-03): LIVE 臂总开关 playbook.live_manual_arm.enabled 是
    # live 新仓第三道前置 (默认 false = HALT_NEW_LIVE)。本 helper 构造 live 前置
    # ⇒ armed 随参 (默认 True = 开火态)。其余键回落真实 ConfigContainer,
    # 不破坏 live_trial / quote-gate 等下游读取。
    import app.core.config_container as _cc
    _real_get_config = _cc.get_config

    def _armed_get_config():
        real = _real_get_config()

        class _Wrap:
            def get_dotted(self, key, default=None):
                if key == "playbook.live_manual_arm":
                    return {
                        "enabled": armed,
                        "mode": "engineering_probe",
                        "max_open_positions": 1,
                        "max_order_qty": 1,
                        "session": "RTH",
                        "probe": {
                            "authorized": armed,
                            "authorization_id": "TEST-AUTH" if armed else "",
                            "direction": "LONG",
                        },
                    }
                return real.get_dotted(key, default)
        return _Wrap()
    monkeypatch.setattr("app.core.config_container.get_config", _armed_get_config)


def _config(monkeypatch, **overrides):
    values = {
        "pretrade_quote_gate_enabled": True,
        "pretrade_max_quote_age_sec": 60.0,
        "pretrade_max_spread_pct": 0.05,
        "entry_freshness_gate_enabled": True,
        "entry_max_quote_age_sec": 90.0,
    }
    values.update(overrides)
    monkeypatch.setattr("app.playbook.config.get_playbook_config", lambda: values)


def _fresh_quote(_symbol):
    return {
        "bid": 100.0, "ask": 100.1, "price": 100.05,
        "timestamp": time.time(), "update_time": datetime.now(timezone.utc).isoformat(),
        "data_source": "futu_lv3", "book_is_proxy": False,
    }


def _assert_not_claimed(svc, iid):
    row = svc._find_instruction_by_id(iid)
    assert row["execution_state"] == "PENDING"
    assert not row["executed"]
    assert int(row["execution_attempt"]) == 0


class TestLiveFailClose:
    @pytest.mark.parametrize("quote", [None, {"price": 0}, {"last": 0}])
    def test_live_without_valid_price_is_always_rejected(self, svc, db, monkeypatch, quote):
        _live_prereqs(monkeypatch)
        _config(
            monkeypatch,
            pretrade_quote_gate_enabled=False,
            entry_freshness_gate_enabled=False,
        )
        iid = _save_inst(db)
        dual, captured = _mk_dual()

        with patch("app.execution.dual_broker.get_dual_broker", return_value=dual), patch(
            "app.playbook.playbook_service._private_router_quote", return_value=quote
        ):
            result = _call_inner(svc, iid, "real")

        assert result["error_code"] == "LIVE_ENTRY_NO_QUOTE"
        assert not captured
        _assert_not_claimed(svc, iid)

    def test_live_last_price_alone_cannot_bypass_book_check(self, svc, db, monkeypatch):
        _live_prereqs(monkeypatch)
        _config(
            monkeypatch,
            pretrade_quote_gate_enabled=False,
            entry_freshness_gate_enabled=False,
        )
        iid = _save_inst(db)
        dual, captured = _mk_dual()

        with patch("app.execution.dual_broker.get_dual_broker", return_value=dual), patch(
            "app.playbook.playbook_service._private_router_quote",
            return_value={"last": 100.0},
        ):
            result = _call_inner(svc, iid, "real")

        assert result.get("c2_quote_health") is True
        assert not captured
        _assert_not_claimed(svc, iid)

    def test_enabled_health_gate_requires_bid_and_ask(self, svc, db, monkeypatch):
        _live_prereqs(monkeypatch)
        _config(monkeypatch, entry_freshness_gate_enabled=False)
        iid = _save_inst(db)
        dual, captured = _mk_dual()

        with patch("app.execution.dual_broker.get_dual_broker", return_value=dual), patch(
            "app.playbook.playbook_service._private_router_quote",
            return_value={"price": 100.0, "timestamp": time.time()},
        ):
            result = _call_inner(svc, iid, "real")

        assert result.get("c2_quote_health") is True
        assert not captured
        _assert_not_claimed(svc, iid)

    def test_disabled_health_gate_still_rejects_live_missing_book(self, svc, db, monkeypatch):
        _live_prereqs(monkeypatch)
        _config(
            monkeypatch,
            pretrade_quote_gate_enabled=False,
            entry_freshness_gate_enabled=False,
        )
        iid = _save_inst(db)
        dual, captured = _mk_dual()

        with patch("app.execution.dual_broker.get_dual_broker", return_value=dual), patch(
            "app.playbook.playbook_service._private_router_quote",
            return_value={"price": 100.0},
        ):
            result = _call_inner(svc, iid, "real")

        assert result.get("c2_quote_health") is True
        assert not captured
        _assert_not_claimed(svc, iid)

    @pytest.mark.parametrize("overrides", [
        {"bid": 100.0, "ask": 100.0},
        {"bid": 100.2, "ask": 100.1},
        {"book_is_proxy": True},
        {"bid": float("nan")},
        {"ask": float("inf")},
        {"price": float("nan")},
        {"update_time": None, "quote_age_ms": 1},
        {"update_time": "invalid", "quote_age_ms": 1},
        {"data_source": "finnhub"},
    ])
    def test_live_rejects_unverifiable_quotes_with_flags_disabled(
        self, svc, db, monkeypatch, overrides,
    ):
        _live_prereqs(monkeypatch)
        _config(monkeypatch, pretrade_quote_gate_enabled=False, entry_freshness_gate_enabled=False)
        iid = _save_inst(db)
        dual, captured = _mk_dual()
        quote = {**_fresh_quote("US.TEST_C2"), **overrides}
        with patch("app.execution.dual_broker.get_dual_broker", return_value=dual), patch(
            "app.playbook.playbook_service._private_router_quote", return_value=quote,
        ):
            result = _call_inner(svc, iid, "real")
        assert result["success"] is False
        assert not captured
        _assert_not_claimed(svc, iid)

    @pytest.mark.parametrize("offset_seconds", [-61, 30])
    def test_live_uses_exchange_clock_not_fresh_local_receipt(
        self, svc, db, monkeypatch, offset_seconds,
    ):
        _live_prereqs(monkeypatch)
        _config(monkeypatch, pretrade_quote_gate_enabled=False, entry_freshness_gate_enabled=False)
        iid = _save_inst(db)
        dual, captured = _mk_dual()
        quote = _fresh_quote("US.TEST_C2")
        quote["update_time"] = (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).isoformat()
        quote["quote_age_ms"] = 0
        with patch("app.execution.dual_broker.get_dual_broker", return_value=dual), patch(
            "app.playbook.playbook_service._private_router_quote", return_value=quote,
        ):
            result = _call_inner(svc, iid, "real")
        assert result["success"] is False
        assert not captured
        _assert_not_claimed(svc, iid)

    def test_live_health_failure_is_rejected(self, svc, db, monkeypatch):
        _live_prereqs(monkeypatch)
        _config(monkeypatch)
        iid = _save_inst(db)
        dual, captured = _mk_dual()

        with patch("app.execution.dual_broker.get_dual_broker", return_value=dual), patch(
            "app.playbook.playbook_service._private_router_quote", side_effect=_fresh_quote
        ), patch(
            "app.risk.data_health_guard.check_quote_health",
            return_value=(False, "spread"),
        ):
            _call_inner(svc, iid, "real")

        assert not captured
        _assert_not_claimed(svc, iid)

    def test_live_health_exception_is_rejected(self, svc, db, monkeypatch):
        _live_prereqs(monkeypatch)
        _config(monkeypatch)
        iid = _save_inst(db)
        dual, captured = _mk_dual()

        with patch("app.execution.dual_broker.get_dual_broker", return_value=dual), patch(
            "app.playbook.playbook_service._private_router_quote", side_effect=_fresh_quote
        ), patch(
            "app.risk.data_health_guard.check_quote_health",
            side_effect=RuntimeError("gate boom"),
        ):
            result = _call_inner(svc, iid, "real")

        assert result.get("c2_gate_error") is True
        assert not captured
        _assert_not_claimed(svc, iid)

    def test_live_freshness_failure_is_rejected(self, svc, db, monkeypatch):
        _live_prereqs(monkeypatch)
        _config(monkeypatch)
        iid = _save_inst(db)
        dual, captured = _mk_dual()

        with patch("app.execution.dual_broker.get_dual_broker", return_value=dual), patch(
            "app.playbook.playbook_service._private_router_quote", side_effect=_fresh_quote
        ), patch(
            "app.risk.data_health_guard.check_quote_health", return_value=(True, "ok")
        ), patch(
            "app.risk.data_health_guard.check_quote_freshness",
            return_value=(False, "stale"),
        ):
            _call_inner(svc, iid, "real")

        assert not captured
        _assert_not_claimed(svc, iid)

    def test_live_freshness_exception_is_rejected(self, svc, db, monkeypatch):
        _live_prereqs(monkeypatch)
        _config(monkeypatch)
        iid = _save_inst(db)
        dual, captured = _mk_dual()

        with patch("app.execution.dual_broker.get_dual_broker", return_value=dual), patch(
            "app.playbook.playbook_service._private_router_quote", side_effect=_fresh_quote
        ), patch(
            "app.risk.data_health_guard.check_quote_health", return_value=(True, "ok")
        ), patch(
            "app.risk.data_health_guard.check_quote_freshness",
            side_effect=RuntimeError("freshness boom"),
        ):
            result = _call_inner(svc, iid, "real")

        assert result.get("c2_gate_error") is True
        assert not captured
        _assert_not_claimed(svc, iid)

    def test_disabled_freshness_gate_is_skipped(self, svc, db, monkeypatch):
        _live_prereqs(monkeypatch)
        _config(monkeypatch, entry_freshness_gate_enabled=False)
        iid = _save_inst(db)
        dual, captured = _mk_dual()

        with patch("app.execution.dual_broker.get_dual_broker", return_value=dual), patch(
            "app.playbook.playbook_service._private_router_quote", side_effect=_fresh_quote
        ), patch(
            "app.risk.data_health_guard.check_quote_health", return_value=(True, "ok")
        ), patch(
            "app.risk.data_health_guard.check_quote_freshness",
            side_effect=AssertionError("disabled gate must not run"),
        ):
            _call_inner(svc, iid, "real")

        assert captured.get("called") is True

    def test_live_healthy_quote_reaches_broker(self, svc, db, monkeypatch):
        _live_prereqs(monkeypatch)
        _config(monkeypatch)
        iid = _save_inst(db)
        dual, captured = _mk_dual()

        with patch("app.execution.dual_broker.get_dual_broker", return_value=dual), patch(
            "app.playbook.playbook_service._private_router_quote", side_effect=_fresh_quote
        ):
            _call_inner(svc, iid, "real")

        assert captured.get("called") is True
        assert captured["target_env"] == "real"

    def test_live_engineering_probe_clamps_order_qty_to_one(self, svc, db, monkeypatch):
        _live_prereqs(monkeypatch)
        _config(
            monkeypatch,
            pretrade_quote_gate_enabled=False,
            entry_freshness_gate_enabled=False,
        )
        iid = _save_inst(db)
        dual, captured = _mk_dual()

        with patch("app.execution.dual_broker.get_dual_broker", return_value=dual), patch(
            "app.playbook.playbook_service._private_router_quote", side_effect=_fresh_quote
        ):
            _call_inner(svc, iid, "real")

        assert captured.get("called") is True
        assert captured["order_qty"] == 1


class TestSimulationUnchanged:
    def test_sim_proxy_with_disabled_gates_still_reaches_broker(self, svc, db, monkeypatch):
        _config(monkeypatch, pretrade_quote_gate_enabled=False, entry_freshness_gate_enabled=False)
        iid = _save_inst(db)
        dual, captured = _mk_dual()
        quote = {"bid": 100.0, "ask": 100.0, "price": 100.0, "book_is_proxy": True}
        with patch("app.execution.dual_broker.get_dual_broker", return_value=dual), patch(
            "app.playbook.playbook_service._private_router_quote", return_value=quote,
        ):
            _call_inner(svc, iid, "sim")
        assert captured["target_env"] == "sim"

    def test_sim_without_quote_remains_fail_open(self, svc, db):
        iid = _save_inst(db)
        dual, captured = _mk_dual()
        with patch("app.execution.dual_broker.get_dual_broker", return_value=dual), patch(
            "app.playbook.playbook_service._private_router_quote", return_value=None
        ):
            _call_inner(svc, iid, "sim")
        assert captured.get("called") is True
        assert captured["target_env"] == "sim"

    @pytest.mark.parametrize(
        "gate",
        ["check_quote_health", "check_quote_freshness"],
    )
    def test_sim_gate_exception_remains_fail_open(self, svc, db, monkeypatch, gate):
        _config(monkeypatch)
        iid = _save_inst(db)
        dual, captured = _mk_dual()
        patches = {
            "check_quote_health": patch(
                "app.risk.data_health_guard.check_quote_health",
                side_effect=RuntimeError("health boom"),
            ),
            "check_quote_freshness": patch(
                "app.risk.data_health_guard.check_quote_freshness",
                side_effect=RuntimeError("freshness boom"),
            ),
        }
        with patch("app.execution.dual_broker.get_dual_broker", return_value=dual), patch(
            "app.playbook.playbook_service._private_router_quote", side_effect=_fresh_quote
        ), patches[gate]:
            _call_inner(svc, iid, "sim")
        assert captured.get("called") is True

    def test_legacy_trade_env_does_not_downgrade_live_to_sim(self, svc, db, monkeypatch):
        _live_prereqs(monkeypatch, db_live=False)
        iid = _save_inst(db)
        dual, captured = _mk_dual()
        with patch("app.execution.dual_broker.get_dual_broker", return_value=dual), patch(
            "app.playbook.playbook_service._private_router_quote", return_value=None
        ):
            result = _call_inner(svc, iid, "real")
        assert result["success"] is False
        assert not captured
        _assert_not_claimed(svc, iid)

    def test_disarmed_live_halt_precedes_half_flip_downgrade(self, svc, db, monkeypatch):
        _live_prereqs(monkeypatch, db_live=False, armed=False)
        iid = _save_inst(db)
        dual, captured = _mk_dual()

        with patch("app.execution.dual_broker.get_dual_broker", return_value=dual), patch(
            "app.playbook.playbook_service._private_router_quote", return_value=None
        ):
            result = _call_inner(svc, iid, "real")

        assert result["success"] is False
        assert any(
            reason["code"] == "LIVE_ARM_NOT_ENABLED"
            for reason in result["reasons"]
        )
        assert not captured
        _assert_not_claimed(svc, iid)


def test_live_open_cap_rejects_before_execution_claim(svc, db, monkeypatch, tmp_path):
    _live_prereqs(monkeypatch)
    ledger = tmp_path / "live-open-cap.sqlite"
    connection = sqlite3.connect(ledger)
    try:
        connection.execute("CREATE TABLE trade_records (source_type TEXT, exit_time TEXT)")
        connection.execute("INSERT INTO trade_records VALUES ('PLAYBOOK_LIVE', '')")
        connection.commit()
    finally:
        connection.close()
    monkeypatch.setattr(
        "app.core.paths.get_trade_records_db_for_arm",
        lambda target_env: ledger,
    )
    iid = _save_inst(db)
    dual, captured = _mk_dual()

    with patch("app.execution.dual_broker.get_dual_broker", return_value=dual):
        result = _call_inner(svc, iid, "real")

    assert result["success"] is False
    assert any(
        reason["code"] == "LIVE_PROBE_OPEN_CAP_REACHED"
        for reason in result["reasons"]
    )
    assert not captured
    _assert_not_claimed(svc, iid)


def test_live_service_gate_enforces_rth(svc, db, monkeypatch):
    _live_prereqs(monkeypatch)
    monkeypatch.setattr(
        "app.core.scheduler_triggers.get_market_phase",
        lambda: SimpleNamespace(value="PRE_MARKET"),
    )
    iid = _save_inst(db)
    dual, captured = _mk_dual()

    with patch("app.execution.dual_broker.get_dual_broker", return_value=dual):
        result = _call_inner(svc, iid, "real")

    assert result["success"] is False
    assert any(
        reason["code"] == "LIVE_MARKET_PHASE_REJECTED"
        for reason in result["reasons"]
    )
    assert not captured
    _assert_not_claimed(svc, iid)


def test_c2_gate_precedes_execution_claim_and_is_absent_from_exit_monitor():
    from app.playbook import pipeline

    entry_source = inspect.getsource(PlaybookService._execute_via_dual_broker_inner)
    exit_source = inspect.getsource(pipeline.monitor_playbook_sim_exits)
    assert entry_source.index("LIVE_ENTRY_NO_QUOTE") < entry_source.index("try_claim_execution")
    assert "LIVE_ENTRY_NO_QUOTE" not in exit_source
    assert "c2_quote_health" not in exit_source


@pytest.mark.parametrize("age_seconds, expected", [(0, True), (59, True), (60, True), (61, False), (-1, False)])
@pytest.mark.parametrize("naive_et", [False, True])
def test_exchange_quote_age_boundary_and_et_contract(monkeypatch, age_seconds, expected, naive_et):
    from zoneinfo import ZoneInfo
    from app.risk import data_health_guard

    now = datetime(2026, 10, 22, 15, 0, tzinfo=timezone.utc)
    stamp = now - timedelta(seconds=age_seconds)
    if naive_et:
        stamp = stamp.astimezone(ZoneInfo("America/New_York")).replace(tzinfo=None)
    quote = {**_fresh_quote("US.TEST_C2"), "update_time": stamp.isoformat()}
    monkeypatch.setattr(data_health_guard, "datetime", SimpleNamespace(now=lambda tz: now))
    actual, _ = data_health_guard.check_quote_health(quote, require_exchange_quote=True)
    assert actual is expected
