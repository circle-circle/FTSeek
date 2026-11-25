"""Offline bounded-entry regressions for reachable context data cache publication."""
import datetime as dt
from enum import Enum
import importlib.util
import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from .test_entry_quote_cache_boundary import env as quote_env


ROOT = Path(__file__).resolve().parents[2]
SYMBOL = "US.AAPL"
PATHS = ("futu_minute", "futu_daily", "router_daily_futu", "router_daily_td",
         "td_daily", "td_ts", "td_spy", "router_spy", "av_live", "av_fred")


class _SdkRows:
    def __init__(self, rows):
        self.iloc = rows

    def __len__(self):
        return len(self.iloc)

    def iterrows(self):
        return iter(enumerate(self.iloc))


class _EntryContext:
    def __init__(self, read):
        self.read = read

    def get_full_market_context(self, symbol):
        assert symbol == SYMBOL
        return self.read()


@pytest.fixture
def env(quote_env, monkeypatch, tmp_path):
    base = quote_env
    monkeypatch.setattr(base.fetch, "DEFAULT_ENTRY_DATA_TIMEOUT_S", 0.2)
    lv3 = sys.modules["app.data.futu_lv3_provider"]
    sdk = sys.modules["futu"]
    releases = []
    clock = [base.fixed_utc.timestamp()]
    monkeypatch.setattr(lv3, "time", SimpleNamespace(time=lambda: clock[0],
                                                   monotonic=time.monotonic, sleep=time.sleep))
    disk_path = tmp_path / "daily.json"
    monkeypatch.setattr(lv3, "_DAILY_DISK_PATH", str(disk_path))
    monkeypatch.setattr(sdk, "KLType", SimpleNamespace(**{k: k for k in
        ("K_1M", "K_3M", "K_5M", "K_15M", "K_30M", "K_60M", "K_DAY")}), raising=False)
    monkeypatch.setattr(sdk, "AuType", SimpleNamespace(NONE="NONE", HFQ="HFQ", QFQ="QFQ"), raising=False)

    def load(name, relative):
        spec = importlib.util.spec_from_file_location(name, ROOT / relative)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        return module

    load("app.utils.log_sanitize", "app/utils/log_sanitize.py")
    load("app.data.vix_source", "app/data/vix_source.py")
    load("app.data.fred_series", "app/data/fred_series.py")
    av = load("app.data.alphavantage_provider", "app/data/alphavantage_provider.py")
    router_module = load("app.data.market_data_router", "app/data/market_data_router.py")
    assert all(module.DATA_CACHE_PUBLICATION_ALLOWED is base.policy.DATA_CACHE_PUBLICATION_ALLOWED
               for module in (lv3, av, router_module))
    monkeypatch.setenv("FINNHUB_API_KEY", "offline-fake-key")

    class Phase(Enum):
        TRADING = "TRADING"
        OVERNIGHT = "OVERNIGHT"

    phase = [Phase.TRADING]
    base.module("app.core.scheduler_triggers", MarketPhase=Phase, get_market_phase=lambda: phase[0])

    class Transport:
        def __init__(self):
            self.offset = 100.0
            self.vixy = 21.3
            self.fred = 17.84
            self.calls = []
            self.block = False
            self.entered = threading.Event()
            self.release = threading.Event()
            self.lock = threading.Lock()
            releases.append(self.release)

        def invoke(self, kind, result):
            publish = base.policy.DATA_CACHE_PUBLICATION_ALLOWED.get()
            with self.lock:
                self.calls.append((kind, publish))
            if self.block and publish is False and kind != "quote":
                self.entered.set()
                assert self.release.wait(3), "test did not release SDK/HTTP"
            return result

        def history(self, **kwargs):
            minute = kwargs["ktype"] == "K_1M"
            rows = []
            for i in range(1, 251):
                stamp = (f"2026-11-20 09:{i % 60:02d}:00" if minute else
                         (dt.date(2026, 11, 19) - dt.timedelta(days=250 - i)).isoformat())
                close = self.offset + i
                rows.append({"time_key": stamp, "open": close - 1, "high": close + 2,
                             "low": close - 2, "close": close, "volume": 1000, "turnover": close * 1000})
            return self.invoke("history", (0, _SdkRows(rows), None))

        def quote(self, symbols):
            row = {"last_price": self.offset + 250, "prev_close_price": 100,
                   "open_price": 100, "high_price": 400, "low_price": 90,
                   "volume": 1000, "turnover": 100000, "update_time": "2026-11-20 10:00:00"}
            return self.invoke("quote", (0, _SdkRows([row])))

        def http(self, url, **kwargs):
            if "finnhub.io/" in url:
                data = {"c": self.vixy, "t": 1795186800}
                kind, text = "live", ""
            elif "fred.stlouisfed.org/" in url:
                data = {}
                kind, text = "fred", f"DATE,VIXCLS\n2026-11-18,16.0\n2026-11-19,{self.fred}\n2026-11-20,.\n"
            else:
                assert url.endswith("/time_series")
                assert kwargs["params"]["interval"] == "1day"
                data = {"values": [{"datetime": "2026-11-19 00:00:00", "open": self.offset + i - 1,
                                    "high": self.offset + i + 2, "low": self.offset + i - 2,
                                    "close": self.offset + i, "volume": 1000} for i in range(1, 251)]}
                kind, text = "ts", ""
            response = SimpleNamespace(status_code=200, text=text, headers={},
                                       raise_for_status=lambda: None, json=lambda: data)
            return self.invoke(kind, response)

        def count(self):
            return sum(kind != "quote" for kind, _ in self.calls)

    def make_case(path):
        transport = Transport()
        monkeypatch.setattr(requests, "get", transport.http)
        futu = lv3.FutuLV3Provider()
        futu._quote_ctx = SimpleNamespace(get_global_state=lambda: (0, {}),
                                         request_history_kline=transport.history,
                                         get_market_snapshot=transport.quote)
        futu._connected = True
        futu._watchdog_started = True
        save = Mock(wraps=futu._daily_disk_save)
        monkeypatch.setattr(futu, "_daily_disk_save", save)
        td = base.td.TwelveDataProvider(api_key="offline-fake-key")
        router = None
        provider_av = None
        maps = []
        if path == "futu_minute":
            read = lambda: futu.get_kline(SYMBOL, count=60)
            maps = [futu._kline_data_cache, futu._push_kline_cache]
        elif path == "futu_daily":
            read = lambda: futu.get_daily_kline(SYMBOL, count=60)
            maps = [futu._daily_kline_cache, futu._daily_kline_cache_time]
        elif path.startswith("router_"):
            router = router_module.MarketDataRouter(twelvedata_api_key="offline-fake-key",
                                                    finnhub_api_key="offline-fake-key", enable_futu_lv3=False)
            router.futu = None if path == "router_daily_td" else futu
            router.td = td
            if path == "router_spy":
                read = router.get_spy_context
                maps = [futu._cache, futu._cache_time]
            else:
                read = lambda: router.get_daily_bars(SYMBOL, count=60)
                maps = [router._daily_bars_cache]
                maps += ([td._cache, td._cache_time] if path == "router_daily_td" else
                         [futu._daily_kline_cache, futu._daily_kline_cache_time])
        elif path.startswith("td_"):
            read = {"td_daily": lambda: td.get_daily_bars(SYMBOL, count=60),
                    "td_ts": lambda: td.get_time_series(SYMBOL),
                    "td_spy": td.get_spy_context}[path]
            maps = [td._cache, td._cache_time]
        else:
            provider_av = av.AlphaVantageProvider(api_key="offline-fake-key")
            phase[0] = Phase.TRADING if path == "av_live" else Phase.OVERNIGHT
            read = provider_av.get_vix_or_none
            maps = [provider_av.cache, provider_av.cache_time]
        return SimpleNamespace(path=path, read=read, source=_EntryContext(read), transport=transport,
                               futu=futu, td=td, router=router, av=provider_av, maps=maps, save=save)

    def join_workers():
        for release in releases:
            release.set()
        base.join_workers()

    yield SimpleNamespace(**{**vars(base), "make_case": make_case, "join_workers": join_workers,
                             "clock": clock, "disk_path": disk_path, "lv3": lv3, "sdk": sdk,
                             "phase": phase, "Phase": Phase})
    join_workers()


def _entry(env, case, scheduler=None):
    return env.fetch.fetch_entry_data(scheduler or SimpleNamespace(), "context", case.source,
                                     "get_full_market_context", SYMBOL)


def _ma_snapshot(case):
    if case.router is None:
        return ()
    return tuple(getattr(case.router, attr) for attr in
                 ("_spy_ma200_cache", "_spy_ma200_cache_time", "_spy_ma20_cache", "_spy_ma50_cache"))


def _snapshot(case):
    return ([dict(mapping) for mapping in case.maps], _ma_snapshot(case),
            case.futu._daily_disk_dirty, case.save.call_count)


def _unchanged(case, snapshot):
    maps, mas, dirty, saves = snapshot
    for mapping, before in zip(case.maps, maps):
        assert mapping.keys() == before.keys()
        assert all(mapping[key] is value for key, value in before.items())
    assert all(current is before for current, before in zip(_ma_snapshot(case), mas))
    assert case.futu._daily_disk_dirty is dirty
    assert case.save.call_count == saves


def _assert_result(case, result, *, healthy=False):
    offset = 200 if healthy else 100
    if case.path in ("av_live", "av_fred"):
        expected = (26.61 if healthy else 18.11) if case.path == "av_live" else (27.84 if healthy else 17.84)
        assert result == expected
    elif case.path in ("router_spy", "td_spy"):
        assert result["spy_price"] == offset + 250
        assert result["spy_ma20"] == offset + 240.5
        assert result["spy_ma50"] == offset + 225.5
        if case.path == "router_spy":
            assert result["spy_ma200"] == offset + 150.5
    elif case.path == "td_ts":
        assert result["price"] == offset + 250
        assert result["ma20"] == offset + 240.5
        assert result["ma50"] == offset + 225.5
        assert result["source"] == "twelvedata"
        assert result["timestamp"] == "2026-11-20T15:00:00"
    else:
        assert result[-1]["close"] == offset + 250
        assert result[-1]["volume"] == 1000
        if case.path != "futu_minute":
            assert result[-1]["time"] == "2026-11-19"


def _assert_published(case):
    assert all(mapping for mapping in case.maps)
    if case.path == "router_spy":
        assert all(value is not None for value in _ma_snapshot(case))
    if case.path in ("futu_daily", "router_daily_futu"):
        assert case.save.call_count == 1
    if case.path.startswith("av_"):
        key = "vix" if case.path == "av_live" else "vix_fred"
        assert case.av.cache[key] == {"vix": 18.11 if key == "vix" else 17.84,
                                      "source": "finnhub_vixy" if key == "vix" else "fred",
                                      "as_of": "2026-11-20" if key == "vix" else "2026-11-19"}
        assert case.av._vix_probe_fail_ts == 0.0


@pytest.mark.parametrize("path", PATHS)
def test_ordinary_context_reads_still_publish_and_reuse(env, path):
    case = env.make_case(path)
    result = case.read()
    _assert_result(case, result)
    _assert_published(case)
    before = _snapshot(case)
    cached = case.read()
    _assert_result(case, cached)
    assert case.transport.count() == 1
    _unchanged(case, before)
    assert all(publish for _, publish in case.transport.calls)


@pytest.mark.parametrize("path", PATHS)
def test_on_time_entry_context_never_publishes_and_retry_reads_fresh(env, path):
    case = env.make_case(path)
    before = _snapshot(case)
    _assert_result(case, _entry(env, case))
    _unchanged(case, before)
    case.transport.offset, case.transport.vixy, case.transport.fred = 200, 31.3, 27.84
    env.clock[0] += 31
    _assert_result(case, _entry(env, case), healthy=True)
    assert case.transport.count() == 2
    _unchanged(case, before)
    assert not env.disk_path.exists()
    assert all(publish is False for _, publish in case.transport.calls)


@pytest.mark.parametrize("path", PATHS)
def test_late_context_payload_never_publishes_or_retimestamps_and_retry_is_fresh(env, path):
    case = env.make_case(path)
    before = _snapshot(case)
    case.transport.block = True
    scheduler = SimpleNamespace()
    start = time.monotonic()
    with pytest.raises(env.fetch.EntryDataUnavailable, match="timeout"):
        _entry(env, case, scheduler)
    assert time.monotonic() - start < 0.5
    assert case.transport.entered.is_set()
    with pytest.raises(env.fetch.EntryDataUnavailable, match="busy"):
        _entry(env, case, scheduler)
    assert len(env.workers) == 1
    _unchanged(case, before)
    env.join_workers()
    _unchanged(case, before)
    assert not env.disk_path.exists()
    case.transport.block = False
    case.transport.offset, case.transport.vixy, case.transport.fred = 200, 31.3, 27.84
    env.clock[0] += 31
    _assert_result(case, _entry(env, case, scheduler), healthy=True)
    assert case.transport.count() == 2
    _unchanged(case, before)


@pytest.mark.parametrize("path", PATHS)
def test_healthy_unrelated_thread_publication_survives_late_entry(env, path):
    case = env.make_case(path)
    case.transport.block = True
    scheduler = SimpleNamespace()
    with pytest.raises(env.fetch.EntryDataUnavailable, match="timeout"):
        _entry(env, case, scheduler)
    assert case.transport.entered.is_set()
    env.clock[0] += 31
    case.transport.offset, case.transport.vixy, case.transport.fred = 200, 31.3, 27.84
    results, errors = [], []

    def healthy_caller():
        try:
            assert env.policy.DATA_CACHE_PUBLICATION_ALLOWED.get() is True
            results.append(case.read())
        except BaseException as error:
            errors.append(error)

    caller = env.ordinary_thread(healthy_caller)
    caller.start()
    caller.join(3)
    assert not caller.is_alive() and errors == []
    _assert_result(case, results[0], healthy=True)
    assert all(case.maps)
    published = _snapshot(case)
    disk = env.disk_path.read_bytes() if env.disk_path.exists() else None
    env.join_workers()
    _unchanged(case, published)
    assert (env.disk_path.read_bytes() if env.disk_path.exists() else None) == disk
    _assert_result(case, _entry(env, case, scheduler), healthy=True)
    _unchanged(case, published)
    assert case.transport.count() == 2
    assert any(publish for _, publish in case.transport.calls)
    assert any(publish is False for _, publish in case.transport.calls)


@pytest.mark.parametrize("path", PATHS)
def test_existing_context_cache_identity_age_and_ttl_are_preserved(env, path):
    case = env.make_case(path)
    original = case.read()
    _assert_result(case, original)
    if path == "futu_minute":
        next(iter(case.futu._kline_data_cache.values()))["ts"] = env.clock[0] - 42
    elif path in ("futu_daily", "router_daily_futu"):
        case.futu._daily_kline_cache_time[SYMBOL] = env.clock[0] - 42
    if path.startswith("td_") or path == "router_daily_td":
        for key in case.td._cache_time:
            case.td._cache_time[key] = dt.datetime.now() - dt.timedelta(seconds=42)
    if path.startswith("router_daily"):
        bars, _ = case.router._daily_bars_cache[SYMBOL]
        case.router._daily_bars_cache[SYMBOL] = (bars, time.time() - 42)
    if path == "router_spy":
        case.router._spy_ma200_cache_time = dt.datetime.now() - dt.timedelta(seconds=42)
    if path.startswith("av_"):
        for key in case.av.cache_time:
            case.av.cache_time[key] = dt.datetime.now() - dt.timedelta(seconds=42)
    before = _snapshot(case)
    result = _entry(env, case)
    _assert_result(case, result)
    _unchanged(case, before)
    assert case.transport.count() == 1
    if path in ("td_ts", "td_spy", "av_live", "av_fred"):
        assert result is original
    elif isinstance(result, list):
        assert result[-1] is original[-1]


def test_existing_futu_push_ring_is_read_without_ttl_promotion(env):
    case = env.make_case("futu_minute")
    ring = [{"time_key": "2026-11-20 09:59:00", "close": i} for i in range(60)]
    case.futu._push_kline_cache[SYMBOL] = ring
    before = _snapshot(case)
    result = _entry(env, case)
    assert result[-1] is ring[-1]
    _unchanged(case, before)
    assert case.transport.count() == 0
    assert case.futu._kline_data_cache == {}
    assert case.read()[-1] is ring[-1]
    assert next(iter(case.futu._kline_data_cache.values()))["data"][-1] is ring[-1]


@pytest.mark.parametrize("dirty", [False, True])
def test_entry_daily_fetch_never_changes_dirty_or_calls_disk_save(env, dirty):
    case = env.make_case("futu_daily")
    case.futu._daily_disk_dirty = dirty
    before = _snapshot(case)
    _assert_result(case, _entry(env, case))
    _unchanged(case, before)
    assert case.save.call_count == 0
    assert not env.disk_path.exists()


def test_existing_disk_daily_cache_hydration_remains_read_only_allowed(env):
    case = env.make_case("futu_daily")
    age = env.clock[0] - 42
    bars = [{"time": "2026-11-19", "close": 350, "volume": 1000} for _ in range(60)]
    env.disk_path.write_text(json.dumps({"version": 1, "symbols": {SYMBOL: {"bars": bars, "fetched_at": age}}}))
    disk = env.disk_path.read_bytes()
    result = _entry(env, case)
    assert result[-1]["close"] == 350
    assert case.futu._daily_kline_cache[SYMBOL][-1] is result[-1]
    assert case.futu._daily_kline_cache_time[SYMBOL] == age
    assert case.transport.count() == 0 and case.save.call_count == 0
    assert case.futu._daily_disk_dirty is False
    assert env.disk_path.read_bytes() == disk


def test_healthy_sdk_push_ring_publication_survives_late_entry_history(env, monkeypatch):
    case = env.make_case("futu_minute")

    class HandlerBase:
        @staticmethod
        def parse_rsp_pb(payload):
            return 0, payload

    for name in ("StockQuoteHandlerBase", "CurKlineHandlerBase", "TickerHandlerBase", "OrderBookHandlerBase"):
        monkeypatch.setattr(env.sdk, name, HandlerBase, raising=False)
    case.futu._quote_ctx.set_handler = Mock()
    case.futu._setup_push_handlers()
    case.transport.block = True
    scheduler = SimpleNamespace()
    with pytest.raises(env.fetch.EntryDataUnavailable, match="timeout"):
        _entry(env, case, scheduler)
    assert case.transport.entered.is_set()
    payload = [{"code": SYMBOL, "time_key": f"2026-11-20 10:{i:02d}:00",
                "open": 499, "high": 502, "low": 498, "close": 500,
                "volume": 1000} for i in range(60)]
    results = []

    def sdk_callback():
        assert env.policy.DATA_CACHE_PUBLICATION_ALLOWED.get() is True
        results.append(case.futu._push_kline_handler.on_recv_rsp(payload))

    caller = env.ordinary_thread(sdk_callback)
    caller.start()
    caller.join(3)
    assert not caller.is_alive() and results == [(0, payload)]
    ring = case.futu._push_kline_cache[SYMBOL]
    assert len(ring) == 60 and ring[-1]["close"] == 500
    before = _snapshot(case)
    env.join_workers()
    _unchanged(case, before)
    assert case.futu._push_kline_cache[SYMBOL] is ring
    assert _entry(env, case, scheduler)[-1] is ring[-1]
    assert case.futu._kline_data_cache == {}
    assert case.transport.count() == 1
    assert case.futu._push_error_count == 0


@pytest.mark.parametrize("path", ["av_live", "av_fred"])
def test_vix_ttl_expiry_still_fetches_without_publishing_entry_result(env, path):
    case = env.make_case(path)
    case.read()
    key = "vix" if path == "av_live" else "vix_fred"
    case.av.cache_time[key] = dt.datetime.now() - dt.timedelta(seconds=61 if path == "av_live" else 3601)
    before = _snapshot(case)
    case.transport.vixy, case.transport.fred = 31.3, 27.84
    _assert_result(case, _entry(env, case), healthy=True)
    assert case.transport.count() == 2
    _unchanged(case, before)
