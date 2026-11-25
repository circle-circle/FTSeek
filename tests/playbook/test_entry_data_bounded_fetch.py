"""Offline real entry-input regressions with explicit external boundary fakes."""
import ast
import builtins
import copy
from datetime import datetime, timezone
import importlib.util
import inspect
import io
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from types import ModuleType, SimpleNamespace
from typing import Any, Dict, Optional
from unittest.mock import Mock

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def env(tmp_path, monkeypatch):
    violations = []
    workers = []
    releases = []
    caller_ident = threading.get_ident()

    def forbidden(kind):
        def fail(*args, **kwargs):
            violations.append(kind)
            raise AssertionError(f"forbidden external boundary: {kind}")
        return fail

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
    raw_open = os.open

    def checked_os_open(path, flags, *args, **kwargs):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            if not in_tmp(path):
                return forbidden(f"os.open write {path}")()
        return raw_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", checked_os_open)
    raw_connect = sqlite3.connect

    def checked_connect(database, *args, **kwargs):
        if os.fspath(database) != ":memory:" and not in_tmp(database):
            return forbidden(f"sqlite {database}")()
        return raw_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", checked_connect)
    monkeypatch.setattr(sqlite3.dbapi2, "connect", checked_connect)

    def module(name, **attrs):
        mod = ModuleType(name)
        mod.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, mod)
        return mod

    for name in ("app", "app.data", "app.playbook", "app.playbook.pipeline_parts", "app.core",
                 "app.execution", "app.risk", "app.observability", "app.utils", "app.us_pipeline",
                 "app.web_dashboard", "app.web_dashboard.routes"):
        module(name, __path__=[])
    futu = module("futu")
    futu.__getattr__ = lambda name: forbidden(f"Futu {name}")
    module("app.observability.progress_beat", progress_ping=Mock())
    module("app.web_dashboard.routes.ws", publish_event_sync=Mock())
    module("app.utils.timezone", now_et=lambda: datetime(2026, 11, 20, 10, tzinfo=timezone.utc))
    module("app.playbook.entry_confirm_gate", defer_pending=lambda *args: False,
           gate_at_execute=lambda *args, **kwargs: "allow", confirm_gate_config=lambda cfg: {})
    module("app.playbook.entry_routing", chase_cap_enforce_enabled=lambda: True,
           parse_raw_semantics=lambda raw: {"chase_cap_price": None},
           chase_cap_violated=lambda *args: False, chase_cap_anchor_headroom_pct=lambda *args: None)
    module("app.playbook.admission", evaluate=lambda payload: None)
    tca = module("app.playbook.execution_tca_shadow", tca_config=lambda cfg: {"enabled": True},
                 resolve_planned_entry=lambda inst: (inst["entry_price"], "test"),
                 build_attempt_row=Mock(return_value=None))

    def load(name, relative_path):
        spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
        mod = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, mod)
        spec.loader.exec_module(mod)
        return mod

    load("app.playbook.entry_band", "app/playbook/entry_band.py")
    load("app.playbook.price_ledger", "app/playbook/price_ledger.py")
    load("app.playbook.open_blackout", "app/playbook/open_blackout.py")
    s1 = load("app.playbook.s1_arm", "app/playbook/s1_arm.py")
    # Database/configuration boundaries are fakes, not missing-module shims.
    monkeypatch.setattr(s1, "cycle_state", lambda *args: {"enabled": False})
    monkeypatch.setattr(s1, "record_real_kill_shadow", Mock())
    load("app.data.quote_cache_policy", "app/data/quote_cache_policy.py")
    fetch = load("app.playbook.entry_data_fetch", "app/playbook/entry_data_fetch.py")
    assert fetch.DEFAULT_ENTRY_DATA_TIMEOUT_S == 3.0
    monkeypatch.setattr(fetch, "DEFAULT_ENTRY_DATA_TIMEOUT_S", 0.04)
    real_thread = threading.Thread

    def tracked_thread(*args, **kwargs):
        worker = real_thread(*args, **kwargs)
        workers.append(worker)
        return worker

    monkeypatch.setattr(fetch.threading, "Thread", tracked_thread)
    trigger = load("app.playbook.pipeline_parts.trigger_flow", "app/playbook/pipeline_parts/trigger_flow.py")
    auto = load("app.playbook.pipeline_parts.auto_execute_flow", "app/playbook/pipeline_parts/auto_execute_flow.py")
    risk = load("app.playbook.pipeline_parts.risk_gates", "app/playbook/pipeline_parts/risk_gates.py")
    logger = Mock()
    regime_context = SimpleNamespace(regime="RISK_ON", vix=15.0)
    regime_assess = Mock(return_value=regime_context)
    regime_factory = Mock(return_value=SimpleNamespace(assess=regime_assess))
    module("app.playbook.market_regime", MarketRegimeGate=regime_factory,
           MarketRegime=SimpleNamespace(NO_TRADE="NO_TRADE"))
    root_boundaries = []

    def root_call(name, value):
        def call(*args, **kwargs):
            assert threading.get_ident() == caller_ident, f"worker called {name}"
            root_boundaries.append(name)
            return value
        return call

    kill = Mock(side_effect=root_call("kill_switch", False))
    daily = Mock(side_effect=root_call("daily_loss", None))
    risk_get_state = Mock(side_effect=root_call("risk_get_state", "NORMAL"))
    risk_factory = Mock(side_effect=root_call("risk_state_constructor", SimpleNamespace(get_state=risk_get_state)))
    module("app.us_pipeline.emergency_interceptor", check_kill_switch=kill)
    module("app.us_pipeline.risk_state_machine", RiskStateMachine=risk_factory,
           RiskState=SimpleNamespace(RISK_OFF="RISK_OFF", HALT="HALT"))
    module("app.core.live_readiness", preflight_fail_mode=lambda: "open")
    module("app.core.paths", get_trade_records_db_for_arm=lambda arm: ":memory:")
    module_file = tmp_path / "app" / "playbook" / "pipeline.py"
    (tmp_path / "data").mkdir()
    router = SimpleNamespace(get_full_market_context=Mock(return_value=None),
                             get_quote=Mock(return_value=None))
    scheduler = SimpleNamespace(data_router=router)
    row = {"id": 1, "date": "2026-11-20", "symbol": "US.AAPL", "direction": "BUY",
           "decision_id": "DA-bounded", "entry_price": 100.0, "stop_loss": 96.0,
           "take_profit": 108.0, "position_size": 40, "confidence": 70,
           "horizon": "short", "triggered": 1, "executed": 0, "skip_reason": "",
           "execution_state": "SUBMITTED", "execution_owner": "prior-owner",
           "execution_attempt": 4, "raw_instruction_json": {}}
    rows = [row]
    writes = []
    def audit_on_caller(*args, **kwargs):
        assert threading.get_ident() == caller_ident, "worker emitted execution audit"

    audit = Mock(side_effect=audit_on_caller)

    def update(iid, values):
        assert threading.get_ident() == caller_ident, "worker wrote instruction"
        writes.append(copy.deepcopy(values))
        next(item for item in rows if item["id"] == iid).update(copy.deepcopy(values))

    db = SimpleNamespace(get_instructions_by_date=lambda today: copy.deepcopy(rows),
                         update_instruction_tracking=update,
                         mark_instruction_guardrail_skip=forbidden("permanent guardrail SKIP"),
                         merge_instruction_metadata=Mock())
    service = SimpleNamespace(execute_instruction=Mock(return_value={"success": True}))
    broker = SimpleNamespace(query_positions=Mock(return_value={"success": True, "positions": []}))
    module("app.playbook.db", route_playbook_db=lambda arm: db)
    module("app.playbook.playbook_service", PlaybookService=lambda database: service)
    broker_factory = Mock(return_value=broker)
    module("app.execution.dual_broker", get_dual_broker=broker_factory)

    class Config(dict):
        def get_horizon_params(self, horizon):
            return {}

    cfg = Config(auto_execute=True, same_day_hard_stop_cooldown=False,
                 premarket_invalidation_hard_gate=False)
    module("app.playbook.config", get_playbook_config=lambda: cfg)
    pilot = load("app.playbook.pilot_policy", "app/playbook/pilot_policy.py")
    monkeypatch.setattr(pilot, "entries_enabled", lambda: True)
    monkeypatch.setattr(pilot, "after_entry_cutoff", lambda *args: False)
    monkeypatch.setattr(pilot, "in_entry_window", lambda *args: True)
    monkeypatch.setattr(pilot, "rebalance_day_status", lambda *args: SimpleNamespace(
        is_rebalance_day=True, reason=""))
    monkeypatch.setattr(pilot, "mom_budget_snapshot", lambda *args: (0, 0.0))
    budget = {"max_deploy": 0.0, "playbook_config": cfg, "budget_limit": 80000.0,
              "risk_base_capital": 100000.0, "daily_notional_cap": 80000.0,
              "daily_notional_cap_pct": 0.8, "max_daily_positions": 16,
              "max_new_per_cycle": 5, "max_open_positions": 5,
              "today_opened_notional": 0.0, "today_opened_count": 0, "open_positions_count": 0}

    def trigger_kwargs():
        kwargs = {name: Mock() for name in inspect.signature(trigger.check_intraday_trigger).parameters
                  if name not in {"scheduler", "inst"}}
        kwargs.update(_now_et=lambda: timezone.utc, _today_et_str=lambda: row["date"],
                      module_file=str(module_file), logger=logger, INVALIDATED_SKIP_REASONS=set())
        for name in ("_CUTOFF_LOGGED", "_low_atr_logged", "_scb_scan_logged", "_LIQUIDITY_LOGGED",
                     "_BORROW_POOL_LOGGED", "_LIMIT_SHADOW_EMITTED"):
            kwargs[name] = set()
        return kwargs

    trigger_call = Mock(return_value=True)

    def run_auto(real_trigger=False, real_preflight=False):
        kwargs = {name: Mock(return_value=None)
                  for name in inspect.signature(auto.run_playbook_auto_execute).parameters
                  if name not in {"scheduler", "force"}}
        kwargs.update(datetime_cls=datetime, _now_et=lambda: timezone.utc,
                      _today_et_str=lambda: row["date"], module_file=str(module_file), logger=logger,
                      INVALIDATED_SKIP_REASONS=set(), _CC_NORM_LOGGED=set(), _CC_NORM_LOGGED_MAX=100,
                      _playbook_cfg=lambda key, default=None: cfg.get(key, default),
                      _load_execution_budget=lambda today: (budget, None),
                      _emit_exec_gate_audit=audit,
                      _check_intraday_trigger=(lambda sched, inst: trigger.check_intraday_trigger(
                          sched, inst, **trigger_kwargs())) if real_trigger else trigger_call)
        if real_preflight:
            kwargs["_auto_execute_preflight"] = lambda today, logger=None: risk._auto_execute_preflight(
                today, logger=logger, check_daily_loss_circuit=daily, entry_data_owner=scheduler)
        result = auto.run_playbook_auto_execute(scheduler, force=True, **kwargs)
        assert result is not None and "error" not in result, result
        assert violations == []
        return result

    def blocked(value=None, error=None):
        entered, release = threading.Event(), threading.Event()
        releases.append(release)

        def query(*args, **kwargs):
            entered.set()
            assert release.wait(3), "test failed to release blocked worker"
            if error is not None:
                raise error
            return value

        return SimpleNamespace(query=query, entered=entered, release=release)

    def join_workers():
        for release in releases:
            release.set()
        for worker in workers:
            if worker.ident is not None:
                worker.join(3)
                assert not worker.is_alive(), "test leaked query/caller worker"

    yield SimpleNamespace(fetch=fetch, auto=auto, trigger=trigger, scheduler=scheduler,
                          router=router, row=row, rows=rows, db=db, service=service,
                          broker=broker, broker_factory=broker_factory, cfg=cfg,
                          pilot=pilot, audit=audit, writes=writes,
                          logger=logger, risk=risk, regime_context=regime_context,
                          regime_factory=regime_factory, regime_assess=regime_assess,
                          root_boundaries=root_boundaries, daily=daily, kill=kill,
                          risk_factory=risk_factory, risk_get_state=risk_get_state,
                          tca=tca, workers=workers, real_thread=real_thread,
                          blocked=blocked, join_workers=join_workers, trigger_kwargs=trigger_kwargs,
                          run_auto=run_auto, tmp_path=tmp_path, violations=violations,
                          load=load, module=module)
    join_workers()
    assert violations == [], "application handler swallowed external-boundary violation"


def test_success_preserves_original_payload_and_timestamps(env):
    payload = {"symbol": "US.AAPL", "price": 101.0, "quote_ts": "2026-11-20T10:00:00-05:00",
               "source_ts_ms": 1795186800123, "nested": {"age_ms": 1234}}
    env.router.get_full_market_context.return_value = payload
    result = env.fetch.fetch_entry_data(env.scheduler, "success", env.router, "get_full_market_context", "US.AAPL")
    assert result is payload
    assert result == payload
    assert env.scheduler._entry_data_query_state.flight is None


@pytest.mark.parametrize("method,phase", [("get_full_market_context", "context"),
                                         ("get_quote", "quote"),
                                         ("query_positions", "account"),
                                         ("get_full_market_context", "connect")])
def test_blocked_read_only_inputs_return_before_worker_completes(env, method, phase):
    block = env.blocked({"quote_ts": "original"})
    # Simulate the nested connector/account wait inside the read-only call,
    # rather than waiting anywhere in trigger or order execution.
    wait_phase = Mock(side_effect=block.query)
    root_ident = threading.get_ident()

    def read_input(*args, **kwargs):
        assert threading.get_ident() != root_ident
        return wait_phase()

    source = SimpleNamespace(**{method: read_input})
    start = time.monotonic()
    with pytest.raises(env.fetch.EntryDataUnavailable) as caught:
        env.fetch.fetch_entry_data(env.scheduler, phase, source, method)
    assert caught.value.reason == "timeout"
    assert block.entered.is_set()
    assert time.monotonic() - start < 0.5
    assert env.scheduler._entry_data_query_state.flight is not None


def test_twelve_concurrent_labels_share_one_worker_and_expiry_does_not_spawn(env):
    block = env.blocked({"price": 100})
    router = SimpleNamespace(get_full_market_context=block.query)
    scheduler = SimpleNamespace()
    barrier = threading.Barrier(12)
    outcomes = []

    def caller(index):
        barrier.wait()
        try:
            env.fetch.fetch_entry_data(scheduler, f"label-{index}", router,
                                       "get_full_market_context", f"US.SYM{index}")
        except env.fetch.EntryDataUnavailable as error:
            outcomes.append(error.reason)

    callers = [env.real_thread(target=caller, args=(index,)) for index in range(12)]
    env.workers.extend(callers)
    for thread in callers:
        thread.start()
    for thread in callers:
        thread.join(1)
        assert not thread.is_alive()
    query_workers = [thread for thread in env.workers if thread.name == "playbook-entry-data"]
    assert len(query_workers) == 1
    assert sorted(outcomes) == ["busy"] * 11 + ["timeout"]
    # Even with a new label/symbol and a longer deadline, no replacement flight.
    start = time.monotonic()
    with pytest.raises(env.fetch.EntryDataUnavailable, match="busy"):
        env.fetch.fetch_entry_data(scheduler, "next-day", router, "get_full_market_context", "US.NEXT", timeout_s=1)
    assert time.monotonic() - start < 0.1
    assert len([thread for thread in env.workers if thread.name == "playbook-entry-data"]) == 1


def test_late_prior_day_result_is_discarded_and_next_symbol_is_fetched_fresh(env):
    old = {"symbol": "US.OLD", "quote_ts": "2026-11-19T10:00:00-05:00", "price": 3}
    block = env.blocked(old)
    env.router.get_full_market_context.side_effect = block.query
    with pytest.raises(env.fetch.EntryDataUnavailable, match="timeout"):
        env.fetch.fetch_entry_data(env.scheduler, "old-day", env.router, "get_full_market_context", "US.OLD")
    flight = env.scheduler._entry_data_query_state.flight
    env.join_workers()
    assert flight.value is None and flight.error is None and flight.abandoned
    fresh = {"symbol": "US.NEW", "quote_ts": "2026-11-20T10:00:00-05:00", "price": 99}
    env.router.get_full_market_context.side_effect = None
    env.router.get_full_market_context.return_value = fresh
    result = env.fetch.fetch_entry_data(env.scheduler, "new-day", env.router, "get_full_market_context", "US.NEW")
    assert result is fresh and result is not old
    assert env.router.get_full_market_context.call_args.args == ("US.NEW",)
    assert env.router.get_full_market_context.call_count == 2


def test_query_exception_is_typed_and_slot_released(env):
    failure = ValueError("fake source down")
    env.router.get_quote.side_effect = failure
    with pytest.raises(env.fetch.EntryDataUnavailable) as caught:
        env.fetch.fetch_entry_data(env.scheduler, "quote", env.router, "get_quote", "US.AAPL")
    assert caught.value.reason == "query_failed" and caught.value.__cause__ is failure
    assert env.scheduler._entry_data_query_state.flight is None
    env.router.get_quote.side_effect = None
    env.router.get_quote.return_value = None
    assert env.fetch.fetch_entry_data(env.scheduler, "retry", env.router, "get_quote", "US.AAPL") is None


def test_thread_creation_failure_releases_slot(env, monkeypatch):
    factory = env.fetch.threading.Thread
    monkeypatch.setattr(env.fetch.threading, "Thread", Mock(side_effect=RuntimeError("cannot create")))
    with pytest.raises(env.fetch.EntryDataUnavailable, match="thread_start_failed"):
        env.fetch.fetch_entry_data(env.scheduler, "create", env.router, "get_quote", "US.AAPL")
    assert env.scheduler._entry_data_query_state.flight is None
    monkeypatch.setattr(env.fetch.threading, "Thread", factory)
    assert env.fetch.fetch_entry_data(env.scheduler, "retry", env.router, "get_quote", "US.AAPL") is None


def test_thread_start_failure_releases_slot(env, monkeypatch):
    factory = env.fetch.threading.Thread
    monkeypatch.setattr(env.fetch.threading, "Thread", lambda **kwargs: SimpleNamespace(
        start=Mock(side_effect=RuntimeError("cannot start"))))
    with pytest.raises(env.fetch.EntryDataUnavailable, match="thread_start_failed"):
        env.fetch.fetch_entry_data(env.scheduler, "start", env.router, "get_quote", "US.AAPL")
    assert env.scheduler._entry_data_query_state.flight is None
    monkeypatch.setattr(env.fetch.threading, "Thread", factory)
    assert env.fetch.fetch_entry_data(env.scheduler, "retry", env.router, "get_quote", "US.AAPL") is None


def test_uninitialized_scheduler_supported(env):
    class Scheduler:
        def __init__(self):
            raise AssertionError("must not need scheduler initialization")
    scheduler = Scheduler.__new__(Scheduler)
    assert env.fetch.fetch_entry_data(scheduler, "new", env.router, "get_quote", "US.AAPL") is None


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), "bad"])
def test_invalid_deadline_never_starts_worker(env, timeout):
    with pytest.raises(env.fetch.EntryDataUnavailable, match="invalid_timeout"):
        env.fetch.fetch_entry_data(env.scheduler, "invalid", env.router, "get_quote", timeout_s=timeout)
    assert env.workers == []


def test_mutating_method_is_not_allowed(env):
    with pytest.raises(env.fetch.EntryDataUnavailable, match="not_read_only"):
        env.fetch.fetch_entry_data(env.scheduler, "bad", env.service, "execute_instruction")
    env.service.execute_instruction.assert_not_called()
    assert env.workers == []


@pytest.mark.parametrize("stage", ["broker", "trigger", "trigger_quote", "knife", "reprice", "mom", "tca"])
@pytest.mark.parametrize("failure", ["timeout", "query_error"])
def test_real_auto_execute_never_submits_after_entry_input_timeout_or_late_completion(env, stage, failure):
    block = env.blocked({"price": 101.0, "prev_close": 100.0, "atr": 3.0,
                         "quote_ts": "2026-11-20T10:00:00-05:00"},
                        error=ValueError("fake input failed") if failure == "query_error" else None)
    if failure == "query_error":
        block.release.set()
    if stage == "broker":
        env.broker.query_positions.side_effect = block.query
    elif stage in {"trigger_quote", "tca"}:
        env.router.get_quote.side_effect = block.query
    else:
        env.router.get_full_market_context.side_effect = block.query
    if stage == "knife":
        env.cfg["premarket_invalidation_hard_gate"] = True
    if stage == "mom":
        env.row["decision_id"] = "MOM-ETF-V1-test"
        assert env.pilot.policy_for(env.row["decision_id"]) is env.pilot.MOM_ETF_V1
    original = copy.deepcopy(env.row)
    start = time.monotonic()
    result = env.run_auto(real_trigger=stage in {"trigger", "trigger_quote"})
    assert result["executed"] == 0
    assert time.monotonic() - start < 0.5
    assert block.entered.is_set(), f"real {stage} boundary was not reached"
    env.service.execute_instruction.assert_not_called()
    assert env.row == original
    assert env.writes == []
    if stage == "broker":
        assert result["skipped"] == len(env.rows)
        assert result["skipped_reason"] == "entry_data_unavailable"
        assert result["query_label"] == "broker_positions"
        env.router.get_full_market_context.assert_not_called()
    if stage not in {"trigger", "trigger_quote"}:
        assert env.audit.call_args.kwargs["action"] == "deferred_entry_data_unavailable"
        extra = env.audit.call_args.kwargs["extra"]
        assert extra["transient"] is True
        assert extra["skip_reason"].startswith("temporary_entry_data_unavailable(")
        expected = {"broker": "broker_positions", "knife": "knife_context",
                    "reprice": "reprice_context", "mom": "mom_context", "tca": "tca_quote"}[stage]
        assert extra["query_label"] == expected
    else:
        telemetry = env.tmp_path / "data" / "trigger_miss_telemetry.jsonl"
        assert "deferred_entry_data_unavailable" in telemetry.read_text()
        if stage == "trigger":
            env.router.get_quote.assert_not_called()
    writes_before, audit_before = copy.deepcopy(env.writes), env.audit.call_count
    env.join_workers()
    env.service.execute_instruction.assert_not_called()
    assert env.writes == writes_before and env.audit.call_count == audit_before
    assert env.row == original


@pytest.mark.parametrize("response", [
    {"success": False, "positions": [], "error": "down"},
    None, {}, {"positions": []}, {"success": True, "positions": None},
    *({"success": True, "positions": rows} for rows in [
        [None], [{}], [{"qty": 1}], [{"code": "US.AAPL"}],
        [{"code": None, "qty": 1}], [{"code": "", "qty": 1}],
        [{"code": 123, "qty": 1}], [{"code": "HK.00700", "qty": 1}],
        [{"code": "US.", "qty": 1}], [{"code": "US. ", "qty": 1}],
        [{"code": "US.AAPL", "qty": None}], [{"code": "US.AAPL", "qty": ""}],
        [{"code": "US.AAPL", "qty": " "}], [{"code": "US.AAPL", "qty": True}],
        [{"code": "US.AAPL", "qty": False}], [{"code": "US.AAPL", "qty": "bad"}],
        [{"code": "US.AAPL", "qty": float("nan")}],
        [{"code": "US.AAPL", "qty": float("inf")}],
        [{"code": "US.AAPL", "qty": float("-inf")}],
        [{"code": "US.AAPL", "qty": "nan"}], [{"code": "US.AAPL", "qty": "inf"}],
        [{"code": "US.AAPL", "qty": []}], [{"code": "US.AAPL", "qty": {}}],
        [{"code": "US.AAPL", "qty": "1e9999"}],
        [{"code": "US.AAPL", "qty": 1}, None],
        [{"code": "US.AAPL", "qty": 1}, {"code": "US.NEXT", "qty": "bad"}],
        *([{"code": code, "qty": 1}] for code in [
            "US.AAPL ", "US. AAPL", "US..AAPL", "US.AAPL\t", "US.AAPL\n",
            "US.\tAAPL", "US.US.AAPL", "US.aapl", "US.Aapl", "us.AAPL",
            "US.AAPL.", "US.-AAPL", "US.AAPL-", "US.AAPL..B", "US.AAPL--B",
            "US.AAPL.-B", "US.AAPL/.B", "US.AAPL_B", " US.AAPL", "US.AAPL\r\n",
            "US.AAPL$",
        ]),
        [{"code": "US.AAPL", "qty": 1}, {"code": "US.NEXT ", "qty": 1}],
    ]),
])
def test_broker_failure_evidence_is_not_empty_positions(env, response, monkeypatch):
    # Input validation must not depend on the timeout tests' 40 ms scheduling margin.
    monkeypatch.setattr(env.fetch, "DEFAULT_ENTRY_DATA_TIMEOUT_S", 0.2)
    # Every malformed row must defer all instructions before any partial processing.
    env.rows.append({**env.row, "id": 2, "symbol": "US.NEXT"})
    original = copy.deepcopy(env.rows)
    env.broker.query_positions.return_value = response
    result = env.run_auto()
    assert result["executed"] == 0 and result["skipped_reason"] == "entry_data_unavailable"
    assert result["skipped"] == len(env.rows) and result["query_label"] == "broker_positions"
    env.service.execute_instruction.assert_not_called()
    env.router.get_full_market_context.assert_not_called()
    env.router.get_quote.assert_not_called()
    assert env.writes == [] and env.rows == original
    reason = ("malformed_positions" if isinstance(response, dict)
              and response.get("success") is True and isinstance(response.get("positions"), list)
              else "query_unsuccessful")
    assert env.audit.call_count == len(env.rows)
    for call in env.audit.call_args_list:
        assert call.kwargs["action"] == "deferred_entry_data_unavailable"
        assert call.kwargs["extra"]["why"] == reason
        assert call.kwargs["extra"]["transient"] is True


@pytest.mark.parametrize("direction", ["BUY", "SELL_SHORT"])
@pytest.mark.parametrize("qty", [3, -3, 0, 2.5, -2.5, "3", "-3", "0", "2.5", "-2.5"])
def test_valid_broker_rows_preserve_signed_quantity_and_direction_aware_held_behavior(env, direction, qty):
    env.row["direction"] = direction
    position = {"code": env.row["symbol"], "qty": qty}
    response = {"success": True, "positions": [position]}
    env.broker.query_positions.return_value = response
    root_ident = threading.get_ident()

    def mark_existing(iid, execution_time):
        assert threading.get_ident() == root_ident
        assert iid == env.row["id"] and execution_time

    # Only the persistence boundary is fake; the actual direction-aware held
    # branch must return/continue rather than falling through SIM fail-open.
    env.db.mark_instruction_existing_position = Mock(side_effect=mark_existing)
    held = float(qty) != 0 and ((float(qty) > 0) == (direction == "BUY"))
    result = env.run_auto()
    assert result["executed"] == (0 if held else 1)
    assert response["positions"] is not None and response["positions"][0] is position
    assert position["qty"] is qty  # validation does not coerce the payload itself
    assert "skipped_reason" not in result
    if held:
        assert result["skipped"] == 1
        env.db.mark_instruction_existing_position.assert_called_once()
        env.service.execute_instruction.assert_not_called()
    else:
        env.db.mark_instruction_existing_position.assert_not_called()
        env.service.execute_instruction.assert_called_once()


@pytest.mark.parametrize("code", ["US.BRK.B", "US.BRK-B", "US.AAPL261218C00150000"])
@pytest.mark.parametrize("direction,qty", [("BUY", 3), ("SELL_SHORT", "-3")])
def test_valid_broker_code_lexical_forms_keep_exact_matching_held_behavior(env, code, direction, qty):
    env.row.update(symbol=code, direction=direction)
    position = {"code": code, "qty": qty}
    positions = [position]
    response = {"success": True, "positions": positions}
    env.broker.query_positions.return_value = response
    root_ident = threading.get_ident()

    def mark_existing(iid, execution_time):
        assert threading.get_ident() == root_ident
        assert iid == env.row["id"] and execution_time

    env.db.mark_instruction_existing_position = Mock(side_effect=mark_existing)
    result = env.run_auto()
    assert result["executed"] == 0 and result["skipped"] == 1
    assert "skipped_reason" not in result
    env.db.mark_instruction_existing_position.assert_called_once()
    env.service.execute_instruction.assert_not_called()
    assert response["positions"] is positions and positions[0] is position
    assert position["code"] is code and position["qty"] is qty
    assert position == {"code": code, "qty": qty}


def test_successful_empty_positions_keeps_existing_trigger_none_behavior(env):
    result = env.run_auto(real_trigger=True)
    assert result["executed"] == 0
    assert "skipped_reason" not in result
    env.router.get_full_market_context.assert_called_once_with("US.AAPL")
    env.router.get_quote.assert_called_once_with("US.AAPL")
    env.service.execute_instruction.assert_not_called()
    assert env.writes == []


@pytest.mark.parametrize("fallback", [False, True])
def test_blackout_shadow_uses_same_bounded_flight_without_late_publication(env, fallback):
    env.row["triggered"] = 0
    block = env.blocked({"price": 100})
    method = env.router.get_quote if fallback else env.router.get_full_market_context
    method.side_effect = block.query
    target = env.tmp_path / "data" / "blackout.jsonl"
    env.auto._blackout_shadow_path = lambda: target
    start = time.monotonic()
    env.auto.emit_blackout_shadow(env.scheduler, env.row["date"],
                                 datetime(2026, 11, 20, 9, 35, tzinfo=timezone.utc), env.logger, set())
    assert time.monotonic() - start < 0.5
    assert block.entered.is_set()
    before = target.read_bytes()
    assert b'"n_quote_error": 1' in before and b'"n_priced": 0' in before
    env.join_workers()
    assert target.read_bytes() == before
    env.service.execute_instruction.assert_not_called()
    assert env.writes == []


@pytest.mark.parametrize("phase", ["constructor", "assess"])
@pytest.mark.parametrize("failure", ["timeout", "query_error"])
def test_real_entry_preflight_bounds_regime_constructor_and_assessment_only(env, phase, failure):
    block = env.blocked(env.regime_context,
                        error=ValueError("regime unavailable") if failure == "query_error" else None)
    if failure == "query_error":
        block.release.set()
    thread_ids = []

    def constructor():
        thread_ids.append(threading.get_ident())
        if phase == "constructor":
            block.query()
        return SimpleNamespace(assess=assessment)

    def assessment(*, publish_cache):
        assert publish_cache is False
        thread_ids.append(threading.get_ident())
        return block.query() if phase == "assess" else env.regime_context

    env.regime_factory.side_effect = constructor
    start = time.monotonic()
    result = env.run_auto(real_preflight=True)
    assert time.monotonic() - start < 0.5
    assert result["executed"] == 0 and result["skipped_reason"] == "entry_data_unavailable"
    assert result["query_label"] == "entry_market_regime"
    assert block.entered.is_set()
    assert env.root_boundaries == ["kill_switch"]
    env.broker.query_positions.assert_not_called()
    env.risk_factory.assert_not_called()
    env.daily.assert_not_called()
    env.service.execute_instruction.assert_not_called()
    if failure == "timeout":
        with pytest.raises(env.fetch.EntryDataUnavailable, match="busy"):
            env.fetch.fetch_entry_data(env.scheduler, "different-label", env.router,
                                       "get_quote", "US.NEXT")
        env.router.get_quote.assert_not_called()
    env.join_workers()
    assert all(ident != threading.get_ident() for ident in thread_ids)
    assert env.root_boundaries == ["kill_switch"]
    env.service.execute_instruction.assert_not_called()
    assert env.writes == []


def test_regime_success_retains_payload_and_other_preflight_gates_on_caller(env):
    result = env.fetch.fetch_entry_market_regime(env.scheduler)
    assert result is env.regime_context
    env.regime_assess.assert_called_once_with(publish_cache=False)
    outcome = env.risk._auto_execute_preflight(
        env.row["date"], logger=env.logger, check_daily_loss_circuit=env.daily,
        entry_data_owner=env.scheduler)
    assert outcome is None
    assert env.root_boundaries == ["kill_switch", "risk_state_constructor", "risk_get_state", "daily_loss"]


@pytest.mark.parametrize("reason", ["regime_no_trade", "risk_state_RISK_OFF", "daily_loss_circuit"])
def test_owner_preflight_preserves_existing_decision_gates(env, reason):
    if reason == "regime_no_trade":
        env.regime_context.regime = "NO_TRADE"
    elif reason == "risk_state_RISK_OFF":
        env.risk_get_state.side_effect = lambda: "RISK_OFF"
    else:
        env.daily.side_effect = lambda today: {"triggered": True}
    result = env.risk._auto_execute_preflight(
        env.row["date"], logger=env.logger, check_daily_loss_circuit=env.daily,
        entry_data_owner=env.scheduler)
    assert result["reason"] == reason
    env.service.execute_instruction.assert_not_called()


def test_no_owner_preflight_keeps_original_root_thread_path(env):
    identities = []

    def constructor():
        identities.append(threading.get_ident())
        return SimpleNamespace(assess=lambda: env.regime_context)

    env.regime_factory.side_effect = constructor
    assert env.risk._auto_execute_preflight(
        env.row["date"], logger=env.logger, check_daily_loss_circuit=env.daily) is None
    assert identities == [threading.get_ident()]
    assert env.workers == []


def test_actual_pipeline_entry_wrapper_supplies_scheduler_owner(env):
    # Execute only the two real forwarding functions, avoiding pipeline's
    # unrelated import-time bootstrap while testing the actual wiring text.
    source = ROOT / "app" / "playbook" / "pipeline.py"
    tree = ast.parse(source.read_text(), filename=str(source))
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name in {"_auto_execute_preflight", "run_playbook_auto_execute"}]
    assert len(functions) == 2
    namespace = {name: Mock(return_value=None)
                 for name in inspect.signature(env.auto.run_playbook_auto_execute).parameters
                 if name.startswith("_")}
    namespace.update(Any=Any, Dict=Dict, Optional=Optional, datetime=datetime,
                     timed_phase=lambda *args, **kwargs: (lambda function: function),
                     __file__=str(env.tmp_path / "app" / "playbook" / "pipeline.py"),
                     logger=env.logger, INVALIDATED_SKIP_REASONS=set(),
                     _now_et=lambda: timezone.utc, _today_et_str=lambda: env.row["date"],
                     _playbook_cfg=lambda key, default=None: env.cfg.get(key, default),
                     _check_daily_loss_circuit=env.daily)
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), namespace)
    block = env.blocked(env.regime_context)
    env.regime_factory.side_effect = block.query
    result = namespace["run_playbook_auto_execute"](env.scheduler, force=True)
    assert result["skipped_reason"] == "entry_data_unavailable"
    assert result["query_label"] == "entry_market_regime"
    assert block.entered.is_set()
    env.broker.query_positions.assert_not_called()
    env.daily.assert_not_called()
    env.service.execute_instruction.assert_not_called()


@pytest.mark.parametrize("fail_mode", ["open", "closed"])
def test_legacy_no_owner_regime_errors_keep_existing_fail_mode(env, monkeypatch, fail_mode):
    monkeypatch.setattr(sys.modules["app.core.live_readiness"], "preflight_fail_mode", lambda: fail_mode)
    env.regime_factory.side_effect = ValueError("source failed")
    result = env.risk._auto_execute_preflight(
        env.row["date"], logger=env.logger, check_daily_loss_circuit=env.daily)
    assert (result is None) if fail_mode == "open" else result["reason"] == "preflight_fail_close"
    assert env.workers == []


def test_successful_entry_quote_preserves_original_timestamps_and_normal_submission(env):
    payload = {"price": 100.0, "quote_ts": "2026-11-20T10:00:00-05:00", "quote_age_ms": 1234}
    env.router.get_quote.return_value = payload
    result = env.run_auto()
    assert result["executed"] == 1
    env.service.execute_instruction.assert_called_once()
    assert env.service.execute_instruction.call_args.kwargs["custom_params"] is None
    assert env.tca.build_attempt_row.call_args.kwargs["ctx"] is payload
    assert payload["quote_ts"] == "2026-11-20T10:00:00-05:00" and payload["quote_age_ms"] == 1234


@pytest.mark.parametrize("stage", ["broker", "knife", "reprice", "mom", "tca"])
def test_deferral_audit_failure_cannot_allow_submission(env, stage):
    env.audit.side_effect = RuntimeError("fake audit disk full")
    failure = ValueError("fake source down")
    if stage == "broker":
        env.broker.query_positions.side_effect = failure
    elif stage == "tca":
        env.router.get_quote.side_effect = failure
    else:
        env.router.get_full_market_context.side_effect = failure
    if stage == "knife":
        env.cfg["premarket_invalidation_hard_gate"] = True
    if stage == "mom":
        env.row["decision_id"] = "MOM-ETF-V1-test"
    assert env.run_auto()["executed"] == 0
    env.service.execute_instruction.assert_not_called()
    assert env.writes == []


def _load_real_regime_with_fake_providers(env):
    """Load real assessment, never import provider/bootstrap implementations."""
    spy = {"spy_price": 110.0, "spy_ma20": 105.0, "spy_ma50": 100.0,
           "spy_ma200": 95.0, "spx_change_percent": 1.0}
    router = SimpleNamespace(get_spy_context=Mock(return_value=spy),
                             get_vix_or_none=Mock(return_value=14.0),
                             get_quote=Mock(return_value={"pre_price": 0.0}))
    av = SimpleNamespace(get_spy_context=Mock(side_effect=AssertionError("unexpected AV fallback")),
                         get_vix_or_none=Mock(side_effect=AssertionError("unexpected AV fallback")))
    env.module("app.data", __path__=[])
    env.module("app.data.market_data_router", MarketDataRouter=object,
               get_market_data_router=lambda: router)
    env.module("app.data.alphavantage_provider", AlphaVantageProvider=object,
               get_alpha_vantage_provider=lambda: av)
    real = env.load("app.playbook.market_regime", "app/playbook/market_regime.py")
    return real, router, av


def test_real_regime_default_cache_publication_and_ttl_reads_are_unchanged(env):
    real, router, av = _load_real_regime_with_fake_providers(env)
    gate = real.MarketRegimeGate(router, av)
    result = gate.assess()
    assert result.regime == real.MarketRegime.RISK_ON and result.score == 8
    published = real.MarketRegimeGate._assess_cache
    assert published[1] is result
    assert gate.assess() is result
    assert gate.assess(publish_cache=False) is result
    assert real.MarketRegimeGate._assess_cache is published
    router.get_spy_context.assert_called_once_with()
    assert env.workers == []


def test_real_entry_regime_cache_hit_preserves_existing_observation_age_and_ttl(env):
    real, router, av = _load_real_regime_with_fake_providers(env)
    context = real.MarketRegimeGate(router, av).assess()
    published = (time.monotonic() - 42.0, context)
    real.MarketRegimeGate._assess_cache = published
    router.get_spy_context.reset_mock()
    router.get_vix_or_none.reset_mock()
    router.get_quote.reset_mock()
    assert env.fetch.fetch_entry_market_regime(env.scheduler) is context
    assert real.MarketRegimeGate._assess_cache is published
    assert real.MarketRegimeGate._assess_cache[0] == published[0]
    assert time.monotonic() - published[0] >= 42.0
    router.get_spy_context.assert_not_called()
    router.get_vix_or_none.assert_not_called()
    router.get_quote.assert_not_called()


def test_real_entry_regime_success_never_publishes_shared_cache(env):
    real, router, _ = _load_real_regime_with_fake_providers(env)
    result = env.fetch.fetch_entry_market_regime(env.scheduler)
    assert result.regime == real.MarketRegime.RISK_ON and result.score == 8
    assert real.MarketRegimeGate._assess_cache is None
    assert env.risk._auto_execute_preflight(
        env.row["date"], logger=env.logger, check_daily_loss_circuit=env.daily,
        entry_data_owner=env.scheduler) is None
    assert real.MarketRegimeGate._assess_cache is None
    assert router.get_spy_context.call_count == 2
    assert env.root_boundaries == ["kill_switch", "risk_state_constructor", "risk_get_state", "daily_loss"]


@pytest.mark.parametrize("phase", ["spy_context", "shadow_quote"])
@pytest.mark.parametrize("newer_root_cache", [False, True])
def test_real_timed_out_regime_never_publishes_or_overwrites_shared_cache(env, phase, newer_root_cache):
    real, router, av = _load_real_regime_with_fake_providers(env)
    if newer_root_cache:
        # Expired cache forces the actual read path; a caller may independently
        # publish a newer value while the abandoned worker is still blocked.
        expired = (time.monotonic() - 1000, SimpleNamespace(vix_uncertain=False))
        real.MarketRegimeGate._assess_cache = expired
    else:
        expired = None
    method = router.get_spy_context if phase == "spy_context" else router.get_quote
    payload = method.return_value
    block = env.blocked(payload)
    method.side_effect = block.query
    outcome = env.run_auto(real_preflight=True)
    assert outcome["skipped_reason"] == "entry_data_unavailable"
    assert outcome["query_label"] == "entry_market_regime" and block.entered.is_set()
    assert real.MarketRegimeGate._assess_cache is expired
    env.service.execute_instruction.assert_not_called()
    env.risk_factory.assert_not_called()
    env.daily.assert_not_called()
    if newer_root_cache:
        newer = (time.monotonic(), SimpleNamespace(vix_uncertain=False, regime=real.MarketRegime.RISK_ON))
        real.MarketRegimeGate._assess_cache = newer
    else:
        newer = None
    env.join_workers()
    assert real.MarketRegimeGate._assess_cache is newer
    assert env.scheduler._entry_data_query_state.flight is None
    assert env.writes == []
    env.service.execute_instruction.assert_not_called()
    if newer_root_cache:
        assert real.MarketRegimeGate(router, av).assess() is newer[1]
    else:
        method.side_effect = None
        method.return_value = ({**payload, "spy_price": 120.0} if phase == "spy_context" else payload)
        fresh = env.fetch.fetch_entry_market_regime(env.scheduler)
        assert fresh.spy_price == (120.0 if phase == "spy_context" else 110.0)
        assert method.call_count == 2
        assert real.MarketRegimeGate._assess_cache is None


@pytest.mark.parametrize("failure", ["timeout", "query_error"])
def test_real_auto_execute_bounds_broker_constructor_and_discards_late_execution(env, failure):
    block = env.blocked(env.broker,
                        error=ValueError("fake broker initialization failed") if failure == "query_error" else None)
    if failure == "query_error":
        block.release.set()
    identities = []

    def construct():
        identities.append(threading.get_ident())
        return block.query()

    env.broker_factory.side_effect = construct
    original = copy.deepcopy(env.row)
    start = time.monotonic()
    outcome = env.run_auto()
    assert time.monotonic() - start < 0.5
    assert outcome["executed"] == 0 and outcome["skipped_reason"] == "entry_data_unavailable"
    assert outcome["query_label"] == "broker_positions" and block.entered.is_set()
    assert identities and all(ident != threading.get_ident() for ident in identities)
    env.broker.query_positions.assert_not_called()
    env.service.execute_instruction.assert_not_called()
    assert env.writes == [] and env.row == original
    audits_before = env.audit.call_count
    if failure == "timeout":
        with pytest.raises(env.fetch.EntryDataUnavailable, match="busy"):
            env.fetch.fetch_entry_market_regime(env.scheduler)
        env.regime_factory.assert_not_called()
        with pytest.raises(env.fetch.EntryDataUnavailable, match="busy"):
            env.fetch.fetch_entry_broker_positions(env.scheduler)
        env.broker_factory.assert_called_once_with()
    env.join_workers()
    assert env.audit.call_count == audits_before and env.writes == []
    assert env.row == original
    env.service.execute_instruction.assert_not_called()
    # A future day/symbol must make a fresh factory/query, not harvest the old
    # constructor's positions result or revive its old auto-execute invocation.
    env.row["date"] = "2026-11-23"
    env.row["symbol"] = "US.NEXT"
    env.broker_factory.side_effect = None
    fresh = {"success": True, "positions": [], "source_ts": "fresh-next-day"}
    env.broker.query_positions.return_value = fresh
    assert env.fetch.fetch_entry_broker_positions(env.scheduler) is fresh
    assert env.broker_factory.call_count == 2
    assert fresh["source_ts"] == "fresh-next-day"
    env.service.execute_instruction.assert_not_called()


def test_broker_factory_success_returns_original_payload_and_runs_only_fixed_sim_read(env):
    root_ident = threading.get_ident()
    payload = {"success": True, "positions": [], "source_ts": "original"}

    def construct():
        assert threading.get_ident() != root_ident
        return env.broker

    def query(*, target_env):
        assert threading.get_ident() != root_ident and target_env == "sim"
        return payload

    env.broker_factory.side_effect = construct
    env.broker.query_positions.side_effect = query
    assert env.fetch.fetch_entry_broker_positions(env.scheduler) is payload
    env.broker_factory.assert_called_once_with()
    env.broker.query_positions.assert_called_once_with(target_env="sim")
    assert payload["source_ts"] == "original"
    env.service.execute_instruction.assert_not_called()
    assert env.writes == []


def test_broker_factory_does_not_accept_arbitrary_sources_or_query_arguments(env):
    method = env.fetch._BROKER_POSITIONS_FACTORY_METHOD
    with pytest.raises(env.fetch.EntryDataUnavailable, match="not_read_only"):
        env.fetch.fetch_entry_data(env.scheduler, "bad", env.broker_factory, method)
    with pytest.raises(env.fetch.EntryDataUnavailable, match="not_read_only"):
        env.fetch.fetch_entry_data(env.scheduler, "bad", None, method, target_env="real")
    env.broker_factory.assert_not_called()
    assert env.workers == []
