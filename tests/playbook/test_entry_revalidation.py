"""Behavioral B/C safety regressions; run with --noconftest for pre-import isolation.

Only generator/pipeline and their pure data/taxonomy modules are loaded from disk.
Service, broker, risk, configuration and notification boundaries are explicit fakes.
No root conftest, application bootstrap, Futu context or production DB is needed.
"""

import builtins
import copy
import importlib.util
import io
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock
from datetime import datetime, timezone

import pytest


ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_BUDGET_DB = "/opt/ftseek/FTSeek-US/data/ftseek_us.db"
INVALID_ATRS = [None, 0, -2.0, float("nan"), float("inf"), -float("inf"), True, False, "bad"]
INVALID_IDS = ["none", "zero", "negative", "nan", "inf", "negative-inf", "true", "false", "string"]
DIRECTIONS = ["BUY", "SELL_SHORT"]
BUCKETS = {
    "low": {"atr_pct_max": 0.02, "sl_multiplier": 0.7,
            "sl_floor_pct": 1.0, "sl_ceil_pct": 2.0,
            "short_widen_pct": 0.3, "tp_multiplier": 1.5},
    "medium": {"atr_pct_max": 0.05, "sl_multiplier": 0.8,
               "sl_floor_pct": 1.5, "sl_ceil_pct": 3.0,
               "short_widen_pct": 0.4, "tp_multiplier": 2.0},
    "high": {"atr_pct_max": 999.0, "sl_multiplier": 0.8,
             "sl_floor_pct": 2.0, "sl_ceil_pct": 5.0,
             "short_widen_pct": 0.5, "tp_multiplier": 2.5},
}


class Config(dict):
    def __init__(self):
        super().__init__(auto_execute=True, premarket_invalidation_hard_gate=False,
                         max_new_positions_per_cycle=5, max_open_positions=5,
                         max_daily_positions=16,
                         daily_notional_cap_pct=0.8, risk_base_capital=100000.0,
                         max_position_pct=0.06, risk_pct=0.5)
        self.horizon = {"vol_buckets": copy.deepcopy(BUCKETS), "rr_ratio": 1.5,
                        "buy_fixed_stop_loss_pct": 2.5,
                        "sell_fixed_stop_loss_pct": 2.0}

    def get_horizon_params(self, horizon):
        return self.horizon

    def get_horizon_top(self, key):
        return None


class MemoryDB:
    """Copy-on-read instruction storage, like the real SQLite row adapter."""

    def __init__(self):
        self.rows = {}
        self.updates = []
        self.fail_skip_write = False

    def ensure_tables(self):
        pass

    def get_instructions_by_date(self, today):
        return [copy.deepcopy(row) for row in self.rows.values()]

    def update_instruction_tracking(self, instruction_id, values):
        self.updates.append((instruction_id, copy.deepcopy(values)))
        if self.fail_skip_write and values.get("skip_reason") == "reprice_atr_unavailable":
            raise RuntimeError("test skip tracking write failed")
        self.rows[instruction_id].update(copy.deepcopy(values))

    def add(self, direction="BUY", **overrides):
        iid = len(self.rows) + 1
        row = {"id": iid, "date": "2026-09-10", "symbol": f"US.TEST_ENTRY_{iid}",
               "direction": direction, "entry_price": 100.0,
               "stop_loss": 96.0 if direction == "BUY" else 104.0,
               "take_profit": 108.0 if direction == "BUY" else 92.0,
               "position_size": 40, "confidence": 70, "horizon": "short",
               "triggered": 0, "executed": 0, "skip_reason": "",
               "execution_state": "PENDING", "execution_attempt": 0,
               "execution_owner": "", "raw_instruction_json": {}}
        row.update(overrides)
        self.rows[iid] = row
        return row


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Install total isolation BEFORE loading any application implementation."""
    violations = []
    db_calls = []
    redirected_budget_calls = []

    def forbidden(kind):
        def block(*args, **kwargs):
            violations.append(kind)
            raise AssertionError(f"forbidden external boundary: {kind}")
        return block

    for name in ("connect", "connect_ex", "send", "sendall", "sendto", "sendmsg"):
        if hasattr(socket.socket, name):
            monkeypatch.setattr(socket.socket, name, forbidden(f"socket.{name}"))
    monkeypatch.setattr(socket, "create_connection", forbidden("socket.create_connection"))
    monkeypatch.setattr(socket, "getaddrinfo", forbidden("socket.getaddrinfo"))
    monkeypatch.setattr(subprocess, "Popen", forbidden("subprocess.Popen"))
    monkeypatch.setattr(os, "system", forbidden("os.system"))

    def in_tmp(path):
        try:
            return Path(os.fsdecode(path)).resolve().is_relative_to(tmp_path.resolve())
        except (TypeError, ValueError):
            return False

    def checked_open(original):
        def open_file(file, mode="r", *args, **kwargs):
            if any(flag in mode for flag in "wax+") and not in_tmp(file):
                return forbidden(f"file write {file}")()
            return original(file, mode, *args, **kwargs)
        return open_file

    monkeypatch.setattr(builtins, "open", checked_open(builtins.open))
    monkeypatch.setattr(io, "open", checked_open(io.open))
    raw_os_open = os.open

    def checked_os_open(path, flags, *args, **kwargs):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            if not in_tmp(path):
                return forbidden(f"os.open write {path}")()
        return raw_os_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", checked_os_open)
    raw_connect = sqlite3.connect
    budget_db = tmp_path / "budget.sqlite"

    def isolated_connect(database, *args, **kwargs):
        # Explicit legacy-path routing precedes the hard allowlist: the original
        # production path is never passed to SQLite, even for a read-only query.
        if os.fspath(database) == PRODUCTION_BUDGET_DB:
            redirected_budget_calls.append(os.fspath(database))
            database = budget_db
        if os.fspath(database) != ":memory:" and not in_tmp(database):
            return forbidden(f"sqlite {database}")()
        db_calls.append(str(database))
        return raw_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", isolated_connect)
    monkeypatch.setattr(sqlite3.dbapi2, "connect", isolated_connect)
    with sqlite3.connect(budget_db) as conn:
        conn.execute("CREATE TABLE trade_records (entry_price REAL, quantity INTEGER, "
                     "source_type TEXT, entry_time TEXT, exit_time TEXT)")

    def module(name, **attrs):
        mod = ModuleType(name)
        mod.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, mod)
        return mod

    # Empty package search paths prevent importing unreviewed application
    # bootstrap code or optional live adapters through a transitive import.
    for name in ("app", "app.playbook", "app.core", "app.execution", "app.risk",
                 "app.us_pipeline", "app.web_dashboard", "app.web_dashboard.routes",
                 "app.utils"):
        module(name, __path__=[])
    futu = module("futu")
    futu.__getattr__ = lambda name: forbidden(f"Futu {name}")
    cfg = Config()
    db = MemoryDB()
    submissions = []

    def execute_instruction(**kwargs):
        submissions.append(copy.deepcopy(kwargs))
        db.update_instruction_tracking(kwargs["instruction_id"], {"executed": 1})
        return {"success": True}

    service = SimpleNamespace(execute_instruction=Mock(side_effect=execute_instruction))
    def query_order_fees(order_ids, target_env="real"):
        fees = {str(order_id): 2.0 for order_id in order_ids}
        return {"success": True, "fees": fees, "rows": []}

    broker = SimpleNamespace(
        get_positions=Mock(return_value=[]),
        query_order_fees=query_order_fees,
    )
    capital = SimpleNamespace(get_available_budget_for_auto=lambda: 80000.0,
                              get_position_multiplier=lambda: 1.0,
                              get_risk_multiplier=lambda: 1.0,
                              _state=SimpleNamespace(current_equity=100000.0))
    module("app.playbook.config", PlaybookConfig=Config, get_playbook_config=lambda: cfg)
    regime = SimpleNamespace(RISK_ON="RISK_ON", CAUTIOUS="CAUTIOUS",
                             RISK_OFF="RISK_OFF", NO_TRADE="NO_TRADE")
    regime_ctx = SimpleNamespace(regime=regime.RISK_ON, vix=15.0)
    module("app.playbook.market_regime", MarketRegime=regime, RegimeContext=SimpleNamespace,
           MarketRegimeGate=lambda: SimpleNamespace(assess=lambda: regime_ctx))
    module("app.us_pipeline.risk_state_machine",
           RiskState=SimpleNamespace(RISK_OFF="RISK_OFF", HALT="HALT"),
           RiskStateMachine=lambda: SimpleNamespace(get_state=lambda: "NORMAL"))
    module("app.us_pipeline.emergency_interceptor", check_kill_switch=lambda: False)
    module("app.playbook.db", PlaybookDB=lambda: db,
           route_playbook_db=lambda target_env: db)
    module("app.playbook.playbook_service", PlaybookService=lambda database: service)
    module("app.core.paths", get_trade_records_db=lambda: budget_db,
           get_trade_records_db_for_arm=lambda target_env: budget_db)
    module("app.core.db_utils", safe_sqlite_connect=isolated_connect)
    atr_provider = SimpleNamespace(get_atr_pct=Mock(return_value=0.03))
    module("app.core.di_container", get_di_container=lambda: SimpleNamespace(
        atr_provider=lambda: atr_provider))
    module("app.utils.timezone", now_et=lambda: datetime.now(timezone.utc))
    module("app.core.config_container", get_config=lambda: SimpleNamespace(
        get_dotted=lambda key, default=None: default))
    module("app.risk.capital_safety", get_capital_safety_system=lambda: capital)
    module("app.execution.dual_broker", get_dual_broker=lambda: broker)
    module("app.web_dashboard.routes.ws", publish_event_sync=Mock())
    module("app.playbook.sltp_verify_shadow", record_sltp_shadow=Mock())
    module("app.playbook.entry_routing", resolve_route=lambda *args: None,
           chase_cap_enforce_enabled=lambda: True,
           parse_raw_semantics=lambda raw: {"chase_cap_price": None},
           chase_cap_violated=lambda *args: False)

    def load(name, relative_path):
        spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
        mod = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, mod)
        spec.loader.exec_module(mod)
        # Any relative telemetry remains in this test's sandbox, not data/.
        mod.__file__ = str(tmp_path / relative_path)
        return mod

    (tmp_path / "data").mkdir()
    data = load("app.playbook.data_collector", "app/playbook/data_collector.py")
    load("app.playbook.condition_taxonomy", "app/playbook/condition_taxonomy.py")
    generator = load("app.playbook.instruction_generator", "app/playbook/instruction_generator.py")
    # P1a 2026-10-08 (DEB-20261008 拆分路线): pipeline 纯函数本体已迁
    # pipeline_parts — 本隔离环境按"只加载已复核纯模块"原则同批纳入,
    # 否则 pipeline 转发器的懒 import 在空包路径下找不到新包。
    # 2026-10-15: exit_reasons (RFC-142 归一化层, 纯映射) — close_flow 顶import它,
    #   不加载则隔离环境 ModuleNotFoundError (该批 10-13 落地时未同步本清单)。
    load("app.risk.exit_reasons", "app/risk/exit_reasons.py")
    # 同批遗漏: live_fee_reconciliation (RFC-142 LIVE 费用对账, 轻模块 json/datetime 级,
    #   close_flow 的 PLAYBOOK_LIVE 分支 import 它; 不加载则 real 臂 fees_est 恒 None)。
    load("app.execution.live_fee_reconciliation",
         "app/execution/live_fee_reconciliation.py")
    load("app.playbook.pipeline_parts", "app/playbook/pipeline_parts/__init__.py")
    load("app.playbook.pipeline_parts.shadow_telemetry",
         "app/playbook/pipeline_parts/shadow_telemetry.py")
    load("app.playbook.pipeline_parts.instruction_builder",
         "app/playbook/pipeline_parts/instruction_builder.py")
    load("app.playbook.pipeline_parts.exit_classification",
         "app/playbook/pipeline_parts/exit_classification.py")
    load("app.playbook.pipeline_parts.bridge_adapters",
         "app/playbook/pipeline_parts/bridge_adapters.py")
    load("app.playbook.pipeline_parts.runtime_context",
         "app/playbook/pipeline_parts/runtime_context.py")
    load("app.playbook.pipeline_parts.portfolio_guardrails",
         "app/playbook/pipeline_parts/portfolio_guardrails.py")
    load("app.playbook.pipeline_parts.risk_gates",
         "app/playbook/pipeline_parts/risk_gates.py")
    load("app.playbook.pipeline_parts.intraday_indicators",
         "app/playbook/pipeline_parts/intraday_indicators.py")
    # RFC-199/200 新增的纯策略/时间模块同样需显式纳入隔离白名单；
    # 否则 overnight_cleanup / trigger_flow 的懒 import 在空 __path__ 下消失。
    load("app.playbook.pilot_policy", "app/playbook/pilot_policy.py")
    load("app.playbook.open_blackout", "app/playbook/open_blackout.py")
    pipeline = load("app.playbook.pipeline", "app/playbook/pipeline.py")
    monkeypatch.setattr(pipeline, "_now_et", lambda: timezone.utc)
    monkeypatch.setattr(pipeline, "_today_et_str", lambda: "2026-09-10")
    monkeypatch.setattr(pipeline, "_playbook_cfg", lambda key, default=None: cfg.get(key, default))
    monkeypatch.setattr(pipeline, "_check_bridge_health_and_alert", Mock())
    monkeypatch.setattr(pipeline, "_check_daily_loss_circuit", lambda today: None)
    monkeypatch.setattr(pipeline, "_resolve_sizing_base", lambda config: 100000.0)
    audit = Mock()
    monkeypatch.setattr(pipeline, "_emit_exec_gate_audit", audit)
    trigger = Mock(return_value=True)
    monkeypatch.setattr(pipeline, "_check_intraday_trigger", trigger)
    router = SimpleNamespace(get_full_market_context=Mock(return_value={
        "price": 100.0, "lv3_bid": 100.0, "lv3_ask": 100.0, "atr": 3.0}))
    scheduler = SimpleNamespace(data_router=router)

    def run():
        result = pipeline.run_playbook_auto_execute(scheduler, force=True)
        assert result is not None and "error" not in result, result
        assert result.get("errors") == [], result
        assert str(budget_db) in db_calls, "daily guard must use the redirected temporary DB"
        assert redirected_budget_calls == [], "daily guard must not call the legacy production DB path"
        assert set(db_calls) == {str(budget_db)}
        assert violations == [], "an outer except swallowed an isolation violation"
        return result

    yield SimpleNamespace(cfg=cfg, db=db, service=service, submissions=submissions,
                          pipeline=pipeline, generator=generator, data=data,
                          scheduler=scheduler, router=router, trigger=trigger,
                          atr_provider=atr_provider, audit=audit, run=run,
                          budget_db=budget_db, violations=violations)
    assert violations == [], "an application exception handler swallowed an external call"


def assert_paused(env, row):
    stored = env.db.rows[row["id"]]
    assert env.submissions == []
    assert stored["executed"] == 0
    assert stored["skip_reason"] == "reprice_atr_unavailable"
    for key in ("entry_price", "stop_loss", "take_profit", "position_size",
                "execution_state", "execution_attempt", "execution_owner"):
        assert stored[key] == row[key], key


@pytest.mark.parametrize("direction", DIRECTIONS)
@pytest.mark.parametrize("atr", INVALID_ATRS, ids=INVALID_IDS)
def test_bucket_rejects_nonpositive_nonfinite_boolean_and_bad_atr(env, direction, atr):
    assert env.generator._compute_volatility_based_sl_tp(
        atr, direction, env.cfg.horizon, env.cfg) is None


@pytest.mark.parametrize("direction", DIRECTIONS)
@pytest.mark.parametrize("atr", INVALID_ATRS, ids=INVALID_IDS)
def test_generator_rejects_invalid_atr_instead_of_synthesizing_risk(env, direction, atr):
    technical = env.data.TechnicalData(price=100.0, bid=100.0, ask=100.0, atr_14=atr)
    result = env.generator.compute_instruction_values(
        {"symbol": "US.TEST_GENERATOR", "direction": direction},
        SimpleNamespace(technical=technical), total_equity=100000.0)
    assert result.get("skip_reason")
    assert "stop_loss" not in result
    assert "position_size" not in result


@pytest.mark.parametrize("direction", DIRECTIONS)
@pytest.mark.parametrize("atr", INVALID_ATRS, ids=INVALID_IDS)
def test_premarket_normalization_does_not_tag_invalid_atr(env, direction, atr):
    env.db.add(direction, market_regime="RISK_ON")
    env.atr_provider.get_atr_pct.return_value = atr
    result = env.pipeline.run_playbook_normalize_vol_buckets()
    assert result == {"tagged": 0, "missing": 1}
    assert env.db.updates == []
    assert "vol_bucket" not in env.db.rows[1]
    assert "atr_pct" not in env.db.rows[1]


@pytest.mark.parametrize("direction", DIRECTIONS)
def test_premarket_valid_ratio_is_tagged_once_without_repricing(env, direction):
    row = copy.deepcopy(env.db.add(direction, market_regime="RISK_ON"))
    assert env.pipeline.run_playbook_normalize_vol_buckets() == {"tagged": 1, "missing": 0}
    assert env.db.rows[1]["vol_bucket"] == "medium"
    assert env.db.rows[1]["atr_pct"] == 0.03
    for key in ("entry_price", "stop_loss", "take_profit", "position_size"):
        assert env.db.rows[1][key] == row[key]
    assert env.pipeline.run_playbook_normalize_vol_buckets() == {"tagged": 0, "missing": 0}
    assert len(env.db.updates) == 1


@pytest.mark.parametrize("direction", DIRECTIONS)
@pytest.mark.parametrize("field", ["atr_14", "atr_percent"])
@pytest.mark.parametrize("atr", INVALID_ATRS, ids=INVALID_IDS)
def test_invalid_fallback_atr_fields_also_pause_entry(env, direction, field, atr):
    row = copy.deepcopy(env.db.add(direction))
    env.router.get_full_market_context.return_value = {"price": 100.0, field: atr}
    assert env.run()["executed"] == 0
    assert_paused(env, row)


@pytest.mark.parametrize("direction", DIRECTIONS)
@pytest.mark.parametrize("atr", INVALID_ATRS, ids=INVALID_IDS)
def test_invalid_atr_defers_real_auto_execute_without_touching_pending_owner(env, direction, atr):
    row = copy.deepcopy(env.db.add(direction, triggered=1, execution_state="SUBMITTED",
                                   execution_attempt=4, execution_owner="other-worker"))
    env.router.get_full_market_context.return_value = {"price": 100.0, "atr": atr}
    result = env.run()
    assert result["executed"] == 0
    assert result["skipped"] == 1
    assert_paused(env, row)


@pytest.mark.parametrize("direction", DIRECTIONS)
@pytest.mark.parametrize("atr", INVALID_ATRS, ids=INVALID_IDS)
def test_invalid_primary_atr_falls_through_to_valid_atr_14(env, direction, atr):
    env.db.add(direction)
    env.router.get_full_market_context.return_value = {"price": 100.0, "atr": atr,
                                                       "atr_14": 3.0, "atr_percent": 8.0}
    assert env.run()["executed"] == 1
    params = env.submissions[0]["custom_params"]
    assert params is not None
    assert params["stop_loss"] == (97.6 if direction == "BUY" else 102.8)
    assert params["take_profit"] == (104.8 if direction == "BUY" else 94.4)


@pytest.mark.parametrize("direction", DIRECTIONS)
@pytest.mark.parametrize("fields", [
    {"atr": 6.0, "atr_14": 8.0, "atr_percent": 9.0},
    {"atr_14": 6.0},
    {"atr": "bad", "atr_14": float("nan"), "atr_percent": 3.0},
    {"atr": False, "atr_14": -1.0, "atr_percent": 3.0},
    {"atr_percent": 3.0},
], ids=["primary-priority", "atr14", "bad-primary-nan-secondary", "invalid-both", "percent"])
def test_atr_units_and_field_priority_produce_identical_sl_tp(env, direction, fields):
    env.db.add(direction)
    # Non-$100 entry distinguishes a dollar ATR from a percentage value.
    env.router.get_full_market_context.return_value = {"price": 200.0, **fields}
    assert env.run()["executed"] == 1
    params = env.submissions[0]["custom_params"]
    assert params is not None
    assert params["entry_price"] == 200.0
    assert params["stop_loss"] == (195.2 if direction == "BUY" else 205.6)
    assert params["take_profit"] == (209.6 if direction == "BUY" else 188.8)
    assert params["position_size"] == 40
    assert params["risk_amount"] == (192.0 if direction == "BUY" else 224.0)


@pytest.mark.parametrize("direction", DIRECTIONS)
@pytest.mark.parametrize("case", ["no-router", "none", "empty", "zero-price", "raises", "bad-price"])
def test_unavailable_market_context_never_falls_back_to_stale_db_order(env, direction, case):
    row = copy.deepcopy(env.db.add(direction, triggered=1))
    if case == "no-router":
        env.scheduler.data_router = None
    elif case == "raises":
        env.router.get_full_market_context.side_effect = RuntimeError("market context unavailable")
    else:
        env.router.get_full_market_context.return_value = {
            "none": None, "empty": {}, "zero-price": {"price": 0, "atr": 3.0},
            "bad-price": {"price": "bad", "atr": 3.0},
        }[case]
    assert env.run()["executed"] == 0
    assert_paused(env, row)


@pytest.mark.parametrize("direction,price", [("BUY", 95.0), ("SELL_SHORT", 105.0)])
def test_missing_atr_cannot_mirror_risk_after_crossing_original_stop(env, direction, price):
    row = copy.deepcopy(env.db.add(direction))
    env.router.get_full_market_context.return_value = {"price": price, "atr": None}
    assert env.run()["executed"] == 0
    assert_paused(env, row)


def test_skip_reason_write_failure_cannot_escape_outer_reprice_exception(env):
    env.db.add(triggered=1)
    env.db.fail_skip_write = True
    env.router.get_full_market_context.return_value = {"price": 100.0}
    result = env.pipeline.run_playbook_auto_execute(env.scheduler, force=True)
    assert env.submissions == []
    assert result.get("executed", 0) == 0
    assert env.db.rows[1]["executed"] == 0


@pytest.mark.parametrize("direction", DIRECTIONS)
def test_atr_pause_is_recoverable_on_next_round(env, direction):
    row = copy.deepcopy(env.db.add(direction))
    env.router.get_full_market_context.return_value = {"price": 100.0}
    assert env.run()["executed"] == 0
    assert_paused(env, row)
    env.router.get_full_market_context.return_value = {"price": 100.0, "atr": 3.0}
    assert env.run()["executed"] == 1
    assert env.run()["executed"] == 0
    assert len(env.submissions) == 1
    assert env.trigger.call_count == 2


def test_historical_trigger_is_rechecked_after_cycle_capacity_deferral(env):
    row = env.db.add()
    env.trigger.side_effect = [True, False, True]
    env.cfg["max_new_positions_per_cycle"] = 0
    first = env.run()
    assert first["executed"] == 0 and first["skipped"] == 1
    assert env.db.rows[row["id"]]["triggered"] == 1
    assert env.submissions == []
    assert any(call.kwargs.get("action") == "deferred_cycle_cap"
               for call in env.audit.call_args_list)
    env.cfg["max_new_positions_per_cycle"] = 1
    second = env.run()
    assert second["executed"] == 0
    assert env.submissions == []
    assert env.db.rows[row["id"]]["triggered"] == 1
    assert env.run()["executed"] == 1
    assert env.run()["executed"] == 0
    assert len(env.submissions) == 1
    assert env.trigger.call_count == 3
    trigger_writes = [values for _, values in env.db.updates if "triggered" in values]
    assert trigger_writes == [{"triggered": 1}]


def test_open_position_cap_recalculates_after_exit(env):
    env.db.add()
    second = env.db.add()
    env.cfg["max_new_positions_per_cycle"] = 5
    env.cfg["max_open_positions"] = 5
    with sqlite3.connect(env.budget_db) as conn:
        for index in range(4):
            conn.execute(
                "INSERT INTO trade_records "
                "(entry_price, quantity, source_type, entry_time, exit_time) "
                "VALUES (100, 10, 'PLAYBOOK_SIM', '2026-09-10 09:30:00', NULL)"
            )

    assert env.run()["executed"] == 1
    with sqlite3.connect(env.budget_db) as conn:
        conn.execute(
            "INSERT INTO trade_records "
            "(entry_price, quantity, source_type, entry_time, exit_time) "
            "VALUES (100, 40, 'PLAYBOOK_SIM', '2026-09-10 10:00:00', NULL)"
        )

    assert env.run()["executed"] == 0
    assert any(call.kwargs.get("action") == "skipped_open_position_cap"
               for call in env.audit.call_args_list)
    assert env.db.rows[second["id"]]["executed"] == 0

    with sqlite3.connect(env.budget_db) as conn:
        row_id = conn.execute(
            "SELECT rowid FROM trade_records WHERE entry_time='2026-09-10 09:30:00' "
            "ORDER BY rowid LIMIT 1"
        ).fetchone()[0]
        conn.execute(
            "UPDATE trade_records SET exit_time='2026-09-10 10:05:00' "
            "WHERE rowid=?",
            (row_id,)
        )

    assert env.run()["executed"] == 1
    assert env.db.rows[second["id"]]["executed"] == 1
    assert len(env.submissions) == 2


@pytest.mark.parametrize("triggered", [0, 1])
def test_false_current_trigger_never_submits_or_rewrites_history(env, triggered):
    row = env.db.add(triggered=triggered)
    env.trigger.return_value = False
    assert env.run()["executed"] == 0
    assert env.submissions == []
    assert env.db.updates == []
    assert row["triggered"] == triggered
    env.trigger.assert_called_once()


@pytest.mark.parametrize("overrides", [
    {"executed": 1},
    {"direction": "SKIP"}, {"skip_reason": "TRIPWIRE_INVALIDATED"},
], ids=["executed", "skip-direction", "invalidated"])
def test_ineligible_instruction_exits_before_current_trigger_check(env, overrides):
    env.db.add(triggered=1, **overrides)
    assert env.run()["executed"] == 0
    env.trigger.assert_not_called()
    assert env.submissions == []
    assert env.db.updates == []


def test_unpersisted_instruction_expires_at_does_not_gate_execution(env):
    env.db.add(triggered=1, expires_at="2000-01-01T00:00:00+00:00")
    assert env.run()["executed"] == 1
    env.trigger.assert_called_once()
    assert len(env.submissions) == 1


@pytest.mark.parametrize("direction", DIRECTIONS)
def test_no_buckets_keeps_existing_fixed_percentage_path(env, direction):
    env.cfg.horizon.pop("vol_buckets")
    env.db.add(direction, stop_loss=0.0, take_profit=0.0)
    env.router.get_full_market_context.return_value = {"price": 100.0}
    assert env.run()["executed"] == 1
    params = env.submissions[0]["custom_params"]
    assert params["stop_loss"] == (97.5 if direction == "BUY" else 102.0)
    assert params["take_profit"] == (103.75 if direction == "BUY" else 97.0)


@pytest.mark.parametrize("direction", DIRECTIONS)
def test_no_buckets_preserves_existing_db_order_when_context_unavailable(env, direction):
    env.cfg.horizon.pop("vol_buckets")
    env.db.add(direction)
    env.scheduler.data_router = None
    assert env.run()["executed"] == 1
    assert env.submissions[0]["custom_params"] is None


@pytest.mark.parametrize("direction", DIRECTIONS)
@pytest.mark.parametrize("atr", [None, 3.0], ids=["missing", "valid"])
def test_global_bucket_configuration_uses_same_atr_contract(env, direction, atr):
    env.cfg["vol_buckets"] = env.cfg.horizon.pop("vol_buckets")
    row = copy.deepcopy(env.db.add(direction))
    env.router.get_full_market_context.return_value = {"price": 100.0, "atr": atr}
    result = env.run()
    if atr is None:
        assert result["executed"] == 0
        assert_paused(env, row)
    else:
        assert result["executed"] == 1
        assert env.submissions[0]["custom_params"]["stop_loss"] == (
            97.6 if direction == "BUY" else 102.8)


@pytest.mark.parametrize("direction", DIRECTIONS)
@pytest.mark.parametrize("atr,sl_long,sl_short,tp_mult", [
    (0.5, 1.0, 1.3, 1.5), (1.8, 1.26, 1.56, 1.5),
    (3.0, 2.4, 2.8, 2.0), (4.5, 3.0, 3.4, 2.0),
    (10.0, 5.0, 5.5, 2.5),
], ids=["low-floor", "low", "medium", "medium-ceiling", "high-ceiling"])
def test_valid_atr_reprice_matches_generator_sl_tp_and_sizing(
        env, direction, atr, sl_long, sl_short, tp_mult):
    env.db.add(direction, position_size=0)
    env.router.get_full_market_context.return_value = {"price": 100.0, "atr": atr}
    technical = env.data.TechnicalData(price=100.0, bid=100.0, ask=100.0, atr_14=atr)
    data = SimpleNamespace(technical=technical)
    generated = env.generator.compute_instruction_values(
        {"symbol": "US.TEST_PARITY", "direction": direction}, data,
        total_equity=100000.0, risk_pct=0.5)
    assert env.run()["executed"] == 1
    params = env.submissions[0]["custom_params"]
    assert params is not None
    sl_pct = sl_long if direction == "BUY" else sl_short
    sign = 1 if direction == "BUY" else -1
    assert params["stop_loss"] == round(100.0 - sign * sl_pct, 2)
    assert params["take_profit"] == round(100.0 + sign * sl_pct * tp_mult, 2)
    for key in ("entry_price", "stop_loss", "take_profit", "position_size"):
        assert params[key] == generated[key], key
