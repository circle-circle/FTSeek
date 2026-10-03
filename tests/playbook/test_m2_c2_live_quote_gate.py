"""C2: only live Playbook entries fail closed on missing or unhealthy market data."""

import inspect
import json
import sqlite3
import threading
import time
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
def svc(db, monkeypatch, tmp_path):
    # These upstream state queries are not the quote-gate contract under test.
    monkeypatch.setattr(
        "app.coordinator.manual_hold_registry.manual_hold_registry.is_auto_blocked",
        lambda symbol: False,
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
    monkeypatch.setattr("app.core.db_utils.is_live_mode", lambda: db_live)
    monkeypatch.setattr(
        "app.playbook.pipeline._check_graduation_gate",
        lambda: {"graduated": True},
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
                    return {"enabled": armed, "max_open_positions": 1,
                            "max_order_qty": 1, "session": "RTH"}
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
    return {"bid": 100.0, "ask": 100.1, "price": 100.05, "timestamp": time.time()}


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

    def test_live_last_price_is_a_valid_basic_quote(self, svc, db, monkeypatch):
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
            _call_inner(svc, iid, "real")

        assert captured.get("called") is True
        assert captured["target_env"] == "real"

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

    def test_disabled_health_gate_skips_missing_book(self, svc, db, monkeypatch):
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
            _call_inner(svc, iid, "real")

        assert captured.get("called") is True

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


class TestSimulationUnchanged:
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

    def test_half_flip_downgrades_to_sim_before_quote_gate(self, svc, db, monkeypatch):
        _live_prereqs(monkeypatch, db_live=False)
        iid = _save_inst(db)
        dual, captured = _mk_dual()
        with patch("app.execution.dual_broker.get_dual_broker", return_value=dual), patch(
            "app.playbook.playbook_service._private_router_quote", return_value=None
        ):
            _call_inner(svc, iid, "real")
        assert captured.get("called") is True
        assert captured["target_env"] == "sim"


def test_c2_gate_precedes_execution_claim_and_is_absent_from_exit_monitor():
    from app.playbook import pipeline

    entry_source = inspect.getsource(PlaybookService._execute_via_dual_broker_inner)
    exit_source = inspect.getsource(pipeline.monitor_playbook_sim_exits)
    assert entry_source.index("LIVE_ENTRY_NO_QUOTE") < entry_source.index("try_claim_execution")
    assert "LIVE_ENTRY_NO_QUOTE" not in exit_source
    assert "c2_quote_health" not in exit_source
