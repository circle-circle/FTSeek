"""Offline real-provider quote/cache regressions with isolated SDK/HTTP boundaries."""
import atexit
import builtins
import datetime as datetime_module
import importlib.util
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
from unittest.mock import Mock

import pytest
import requests


ROOT = Path(__file__).resolve().parents[2]
SYMBOL = "US.AAPL"
UPDATE_TIME = "2026-11-20 10:00:00"
UPSTREAM_TS = 1795186800.0
PATHS = ("futu_snapshot", "futu_subscribed", "futu_fallback", "futu_td_fallback",
         "finnhub", "twelvedata")


class _SdkTable:
    def __init__(self, row):
        self.iloc = [row]

    def __len__(self):
        return len(self.iloc)


@pytest.fixture
def env(monkeypatch, tmp_path):
    # C-extension datetime types must load before freezing their base class.
    importlib.import_module("pandas")
    violations, workers, other_threads, completions, releases = [], [], [], [], []
    real_thread = threading.Thread

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
    monkeypatch.setattr(requests.sessions.Session, "send", forbidden("requests.Session.send"))

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

    # Avoid package/bootstrap effects without replacing any provider quote/cache
    # implementation. Real pure utilities, calendar and quota limiter are loaded.
    for name in ("app", "app.data", "app.utils", "app.core", "app.playbook",
                 "app.execution", "scripts"):
        module(name, __path__=[])
    monkeypatch.delitem(sys.modules, "app.core.scheduler_triggers", raising=False)
    monkeypatch.setattr(sys, "path", list(sys.path))
    module("dotenv", load_dotenv=lambda *args, **kwargs: None)
    module("scripts.api_limit_monitor", record_finnhub_call=Mock(),
           record_twelvedata_call=Mock(), should_use_twelvedata=lambda: True)
    futu = module("futu", __path__=[], CurKlineHandlerBase=object)
    futu.__getattr__ = lambda name: forbidden(f"unexpected Futu SDK {name}")
    common = module("futu.common", __path__=[])
    futu.common = common
    ft_logger = module("futu.common.ft_logger", logger=SimpleNamespace(file_level=0, console_level=0))
    common.ft_logger = ft_logger
    module("futu.common.sys_config", SysConfig=SimpleNamespace(set_all_thread_daemon=lambda value: None))

    real_datetime = datetime_module.datetime
    fixed_utc = real_datetime(2026, 11, 20, 15, tzinfo=datetime_module.timezone.utc)

    class FrozenDatetime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            value = fixed_utc.replace(tzinfo=None) if tz is None else fixed_utc.astimezone(tz)
            return cls.fromtimestamp(value.timestamp(), tz) if tz is not None else cls(
                value.year, value.month, value.day, value.hour, value.minute, value.second)

    monkeypatch.setattr(datetime_module, "datetime", FrozenDatetime)

    def load(name, relative_path):
        spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
        mod = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, mod)
        spec.loader.exec_module(mod)
        return mod

    policy = load("app.data.quote_cache_policy", "app/data/quote_cache_policy.py")
    fetch = load("app.playbook.entry_data_fetch", "app/playbook/entry_data_fetch.py")
    monkeypatch.setattr(fetch, "DEFAULT_ENTRY_DATA_TIMEOUT_S", 0.2)
    load("app.utils.symbol_utils", "app/utils/symbol_utils.py")
    load("app.utils.timezone", "app/utils/timezone.py")
    load("app.utils.holiday_calendar", "app/utils/holiday_calendar.py")
    load("app.data.finnhub_limiter", "app/data/finnhub_limiter.py")
    fh = load("app.data.finnhub_provider", "app/data/finnhub_provider.py")
    td = load("app.data.twelvedata_provider", "app/data/twelvedata_provider.py")
    lv3 = load("app.data.futu_lv3_provider", "app/data/futu_lv3_provider.py")
    # Freeze only provider wall-clock timestamps; deadline/worker synchronization
    # keeps the real monotonic clock. SDK health checks remain actual methods.
    monkeypatch.setattr(lv3, "time", SimpleNamespace(time=lambda: fixed_utc.timestamp(),
                                                  monotonic=time.monotonic, sleep=time.sleep))
    assert all(mod.DATA_CACHE_PUBLICATION_ALLOWED is policy.DATA_CACHE_PUBLICATION_ALLOWED
               for mod in (fetch, fh, td, lv3))
    assert policy.DATA_CACHE_PUBLICATION_ALLOWED.get() is True

    def tracked_thread(*args, **kwargs):
        target = kwargs["target"]

        def run():
            try:
                target()
            finally:
                completions.append(policy.DATA_CACHE_PUBLICATION_ALLOWED.get())

        worker = real_thread(*args, **{**kwargs, "target": run})
        workers.append(worker)
        return worker

    monkeypatch.setattr(fetch.threading, "Thread", tracked_thread)

    class Transport:
        def __init__(self):
            self.price = 101.0
            self.calls = []
            self.lock = threading.Lock()
            self.block_kind = None
            self.entered = threading.Event()
            self.release = threading.Event()
            releases.append(self.release)
            self.snapshot_failure = False

        def invoke(self, kind, payload):
            publish = policy.DATA_CACHE_PUBLICATION_ALLOWED.get()
            with self.lock:
                self.calls.append((kind, publish, threading.get_ident()))
            if kind == self.block_kind and publish is False:
                self.entered.set()
                assert self.release.wait(3), "test failed to release transport"
            return payload

        def sdk_quote(self, kind, symbols):
            assert symbols == [SYMBOL]
            row = {"last_price": self.price, "prev_close_price": 100.0,
                   "open_price": 100.0, "high_price": self.price, "low_price": 99.0,
                   "volume": 123.0, "turnover": 12300.0, "update_time": UPDATE_TIME}
            payload = (-1, None) if self.snapshot_failure else (0, _SdkTable(row))
            return self.invoke(kind, payload)

        def http_get(self, url, *, params, **kwargs):
            assert url.endswith("/quote") and params == {"symbol": "AAPL"}
            if "finnhub.io/" in url:
                kind = "finnhub"
                data = {"c": self.price, "pc": 100, "o": 100, "h": self.price,
                        "l": 99, "dp": 1, "t": UPSTREAM_TS}
            else:
                assert "api.twelvedata.com/" in url
                kind = "twelvedata"
                data = {"close": self.price, "previous_close": 100, "open": 100,
                        "high": self.price, "low": 99, "percent_change": 1, "volume": 123}
            response = SimpleNamespace(status_code=200, headers={},
                                       raise_for_status=lambda: None, json=lambda: data)
            return self.invoke(kind, response)

        def count(self, kind):
            return sum(call[0] == kind for call in self.calls)

    def make_source(path):
        transport = Transport()
        monkeypatch.setattr(requests, "get", transport.http_get)
        finnhub = fh.FinnhubProvider(api_key="offline-fake-key")
        twelve = td.TwelveDataProvider(api_key="offline-fake-key")
        if path == "finnhub":
            return SimpleNamespace(source=finnhub, caches=[(finnhub, f"fh_quote_{SYMBOL}")],
                                   transport=transport, kind="finnhub")
        if path == "twelvedata":
            return SimpleNamespace(source=twelve, caches=[(twelve, f"quote_{SYMBOL}")],
                                   transport=transport, kind="twelvedata")
        fallback = twelve if path == "futu_td_fallback" else finnhub
        provider = lv3.FutuLV3Provider(fallback_provider=fallback)
        provider._quote_ctx = SimpleNamespace(
            get_global_state=lambda: (0, {}),
            get_stock_quote=lambda symbols: transport.sdk_quote("subscribed", symbols),
            get_market_snapshot=lambda symbols: transport.sdk_quote("snapshot", symbols))
        provider._connected = True
        provider._watchdog_started = True  # no unrelated health daemon needed
        if path == "futu_subscribed":
            provider._subscribed_symbols.add(SYMBOL)
        caches = [(provider, f"quote:{SYMBOL}")]
        kind = "subscribed" if path == "futu_subscribed" else "snapshot"
        if path in ("futu_fallback", "futu_td_fallback"):
            transport.snapshot_failure = True
            kind = "twelvedata" if path == "futu_td_fallback" else "finnhub"
            caches.append((fallback, f"quote_{SYMBOL}" if kind == "twelvedata" else f"fh_quote_{SYMBOL}"))
        return SimpleNamespace(source=provider, caches=caches, transport=transport, kind=kind)

    def join_workers():
        for release in releases:
            release.set()
        for worker in workers + other_threads:
            if worker.ident is not None:
                worker.join(3)
                assert not worker.is_alive(), "test leaked a worker/caller"
        assert completions == [True] * len(workers), "worker did not reset policy"

    def ordinary_thread(target):
        worker = real_thread(target=target, daemon=True)
        other_threads.append(worker)
        return worker

    yield SimpleNamespace(fetch=fetch, policy=policy, make_source=make_source, workers=workers,
                          join_workers=join_workers, ordinary_thread=ordinary_thread,
                          fixed_utc=fixed_utc, fh=fh, td=td, module=module)
    join_workers()
    atexit.unregister(lv3._atexit_release_futu_lv3)
    assert violations == [], "application handler swallowed an external-boundary violation"


def _assert_no_quote_publication(case):
    for provider, key in case.caches:
        assert key not in provider._cache
        assert key not in provider._cache_time


def _assert_source_timestamps(path, quote):
    if path in ("futu_snapshot", "futu_subscribed"):
        assert quote["update_time"] == UPDATE_TIME
        assert quote["timestamp"] == UPSTREAM_TS
        assert quote["data_source"] == ("futu_lv3_subscribed" if path == "futu_subscribed" else "futu_lv3")
    elif path in ("finnhub", "futu_fallback"):
        assert quote["timestamp"] == UPSTREAM_TS
        assert quote["source"] == "finnhub"
        if path == "futu_fallback":
            assert quote["data_source"] == "finnhub_fallback"
    else:
        assert quote["timestamp"] == "2026-11-20T15:00:00"
        assert quote["source"] == "twelvedata"


@pytest.mark.parametrize("path", PATHS)
def test_default_real_provider_quote_calls_still_publish_and_reuse_cache(env, path):
    case = env.make_source(path)
    quote = case.source.get_quote(SYMBOL)
    assert quote["price"] == 101.0
    _assert_source_timestamps(path, quote)
    for provider, key in case.caches:
        assert provider._cache[key] is quote
        assert key in provider._cache_time
    assert case.source.get_quote(SYMBOL) is quote
    assert case.transport.count(case.kind) == 1
    assert all(call[1] is True for call in case.transport.calls)


@pytest.mark.parametrize("path", PATHS)
def test_fast_entry_quote_success_preserves_source_but_never_publishes(env, path):
    case = env.make_source(path)
    scheduler = SimpleNamespace()
    quote = env.fetch.fetch_entry_data(scheduler, "quote", case.source, "get_quote", SYMBOL)
    assert quote["price"] == 101.0
    _assert_source_timestamps(path, quote)
    _assert_no_quote_publication(case)
    case.transport.price = 202.0
    fresh = env.fetch.fetch_entry_data(scheduler, "retry", case.source, "get_quote", SYMBOL)
    assert fresh is not quote and fresh["price"] == 202.0
    assert case.transport.count(case.kind) == 2
    assert all(call[1] is False for call in case.transport.calls)
    _assert_no_quote_publication(case)
    assert env.policy.DATA_CACHE_PUBLICATION_ALLOWED.get() is True


@pytest.mark.parametrize("path", PATHS)
def test_timed_out_real_provider_quote_never_caches_late_result_and_retry_requests_fresh(env, path):
    case = env.make_source(path)
    case.transport.block_kind = case.kind
    scheduler = SimpleNamespace()
    start = time.monotonic()
    with pytest.raises(env.fetch.EntryDataUnavailable, match="timeout"):
        env.fetch.fetch_entry_data(scheduler, "quote", case.source, "get_quote", SYMBOL)
    assert time.monotonic() - start < 0.5
    assert case.transport.entered.is_set()
    assert scheduler._entry_data_query_state.flight is not None
    assert case.transport.count(case.kind) == 1
    with pytest.raises(env.fetch.EntryDataUnavailable, match="busy"):
        env.fetch.fetch_entry_data(scheduler, "next-round", case.source, "get_quote", SYMBOL)
    assert len(env.workers) == 1 and case.transport.count(case.kind) == 1
    _assert_no_quote_publication(case)
    env.join_workers()
    _assert_no_quote_publication(case)
    assert scheduler._entry_data_query_state.flight is None
    case.transport.block_kind = None
    case.transport.price = 202.0
    fresh = env.fetch.fetch_entry_data(scheduler, "retry", case.source, "get_quote", SYMBOL)
    assert fresh["price"] == 202.0  # abandoned SDK/HTTP response contained 101
    assert case.transport.count(case.kind) == 2  # no harvest/reuse of that result
    _assert_source_timestamps(path, fresh)
    _assert_no_quote_publication(case)


@pytest.mark.parametrize("path", PATHS)
def test_entry_cache_hit_preserves_original_object_observation_age_and_ttl(env, path):
    case = env.make_source(path)
    original = case.source.get_quote(SYMBOL)
    ages = []
    for provider, key in case.caches:
        age = (env.fixed_utc.timestamp() - 2 if key.startswith("quote:") else
               datetime_module.datetime.now() - datetime_module.timedelta(seconds=42))
        provider._cache_time[key] = age
        ages.append(age)
    result = env.fetch.fetch_entry_data(SimpleNamespace(), "cached", case.source, "get_quote", SYMBOL)
    assert result is original
    _assert_source_timestamps(path, result)
    assert case.transport.count(case.kind) == 1
    for (provider, key), age in zip(case.caches, ages):
        assert provider._cache[key] is original
        assert provider._cache_time[key] is age
    assert case.source.get_quote(SYMBOL) is original
    assert case.transport.count(case.kind) == 1


@pytest.mark.parametrize("path", PATHS)
def test_abandoned_entry_does_not_overwrite_or_delete_unrelated_thread_quote_publication(env, path):
    case = env.make_source(path)
    case.transport.block_kind = case.kind
    scheduler = SimpleNamespace()
    with pytest.raises(env.fetch.EntryDataUnavailable, match="timeout"):
        env.fetch.fetch_entry_data(scheduler, "quote", case.source, "get_quote", SYMBOL)
    assert case.transport.entered.is_set()
    case.transport.price = 202.0
    ordinary = []
    errors = []

    def caller():
        try:
            assert env.policy.DATA_CACHE_PUBLICATION_ALLOWED.get() is True
            ordinary.append(case.source.get_quote(SYMBOL))
        except BaseException as error:
            errors.append(error)

    thread = env.ordinary_thread(caller)
    thread.start()
    thread.join(3)
    assert not thread.is_alive() and errors == []
    healthy = ordinary[0]
    assert healthy["price"] == 202.0
    published = [(provider._cache[key], provider._cache_time[key]) for provider, key in case.caches]
    assert all(value is healthy for value, _ in published)
    env.join_workers()
    for (provider, key), (value, age) in zip(case.caches, published):
        assert provider._cache[key] is value
        assert provider._cache_time[key] is age
    # An ordinary healthy publication is allowed to be read, without resetting
    # its source observation time or replacing it with the late entry result.
    assert env.fetch.fetch_entry_data(scheduler, "retry", case.source, "get_quote", SYMBOL) is healthy
    assert case.transport.count(case.kind) == 2
    assert any(call[1] is True for call in case.transport.calls)
    assert any(call[1] is False for call in case.transport.calls)


@pytest.mark.parametrize("factory", ["broker", "regime"])
def test_entry_policy_covers_factory_acquisition_and_nested_real_quote_calls(env, factory):
    case = env.make_source("futu_snapshot")
    constructor_policy = []

    def acquire():
        constructor_policy.append(env.policy.DATA_CACHE_PUBLICATION_ALLOWED.get())
        quote = case.source.get_quote(SYMBOL)
        return SimpleNamespace(query_positions=lambda **kwargs: {"success": True, "positions": [], "quote": quote})

    if factory == "broker":
        env.module("app.execution.dual_broker", get_dual_broker=acquire)
        result = env.fetch.fetch_entry_broker_positions(SimpleNamespace())
        assert result["quote"]["price"] == 101.0
    else:
        class Gate:
            def __init__(self):
                constructor_policy.append(env.policy.DATA_CACHE_PUBLICATION_ALLOWED.get())

            def assess(self, *, publish_cache):
                assert publish_cache is False
                return case.source.get_quote(SYMBOL)

        env.module("app.playbook.market_regime", MarketRegimeGate=Gate)
        assert env.fetch.fetch_entry_market_regime(SimpleNamespace())["price"] == 101.0
    assert constructor_policy == [False]
    assert all(call[1] is False for call in case.transport.calls)
    _assert_no_quote_publication(case)


@pytest.mark.parametrize("provider_name", ["finnhub", "twelvedata"])
def test_non_quote_set_cache_obeys_only_reachable_provider_policy(env, provider_name):
    case = env.make_source(provider_name)
    value = {"daily_or_fundamental": "unchanged"}
    token = env.policy.DATA_CACHE_PUBLICATION_ALLOWED.set(False)
    try:
        case.source._set_cache("non_quote", value)
    finally:
        env.policy.DATA_CACHE_PUBLICATION_ALLOWED.reset(token)
    if provider_name == "finnhub":
        assert case.source._cache["non_quote"] is value
        assert "non_quote" in case.source._cache_time
    else:
        assert "non_quote" not in case.source._cache
        assert "non_quote" not in case.source._cache_time
