"""K 线时敏路径: 首次阻塞隔离、单刷新槽、失败冷却与缓存口径契约。"""
import json
import threading
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, mock_open
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from futu import AuType, KLType

from app.data import futu_lv3_provider as provider_module
from app.data.futu_lv3_provider import _KLINE_SICK_WINDOW_SEC, FutuLV3Provider


def _rows(n=12, price=100.0):
    day = datetime.now(ZoneInfo("US/Eastern")).strftime("%Y-%m-%d")
    return [{"time_key": f"{day} 09:{31 + i:02d}:00", "open": price,
             "high": price + 1, "low": price - 1, "close": price,
             "volume": 10.0, "turnover": price * 10} for i in range(n)]


def _join_workers(workers):
    for thread in workers.threads:
        if thread.ident is not None:
            thread.join(timeout=2)
    assert not any(t.is_alive() for t in workers.threads), "测试线程未退出"


@pytest.fixture(autouse=True)
def workers(monkeypatch):
    state = SimpleNamespace(threads=[], release=threading.Event())

    def new_thread(*args, **kwargs):
        thread = threading.Thread(*args, **kwargs)
        state.threads.append(thread)
        return thread

    # 只替换 provider 的模块引用, 不影响 pytest/SDK 使用的 threading。
    monkeypatch.setattr(provider_module, "threading", SimpleNamespace(
        Thread=new_thread, Lock=threading.Lock,
    ))
    try:
        yield state
    finally:
        state.release.set()
        _join_workers(state)


@pytest.fixture
def provider():
    p = FutuLV3Provider()
    p._ensure_connected = Mock(return_value=True)
    p._wait_kline_quota = Mock(return_value=True)
    p._quote_ctx = SimpleNamespace(request_history_kline=Mock(
        return_value=(0, pd.DataFrame(_rows()), ""),
    ))
    p.force_reconnect = Mock()
    return p


@pytest.mark.parametrize("blocked_at", ["connect", "sdk"])
def test_first_and_multi_symbol_calls_do_not_wait(provider, workers, blocked_at):
    entered = threading.Event()
    calling_threads = []

    def block():
        calling_threads.append(threading.current_thread())
        entered.set()
        # 超时仅防旧同步实现把测试永久挂死, 断言门槛远小于该上限。
        assert workers.release.wait(timeout=5)

    if blocked_at == "connect":
        def connect():
            block()
            return True
        provider._ensure_connected.side_effect = connect
    else:
        def request(**kwargs):
            block()
            return 0, pd.DataFrame(_rows()), ""
        provider._quote_ctx.request_history_kline.side_effect = request

    start = time.monotonic()
    assert provider.get_kline("US.AAPL", autype="NONE", latency_sensitive=True) is None
    assert time.monotonic() - start < 0.5
    assert entered.wait(timeout=1)
    assert not workers.release.is_set()
    assert provider._kline_refresh_lock.locked()

    # 即使病窗到期, 悬挂中的 SDK 也不能让其它 symbol 再起 worker。
    provider._kline_sick_until = time.time() - 1
    start = time.monotonic()
    for symbol in ["US.AAPL"] + [f"US.TEST{i}" for i in range(30)]:
        assert provider.get_kline(symbol, autype="NONE", latency_sensitive=True) is None
    assert time.monotonic() - start < 0.5
    assert len(workers.threads) == 1
    assert workers.threads[0].daemon
    assert calling_threads == [workers.threads[0]]
    assert calling_threads[0] is not threading.current_thread()
    provider._ensure_connected.assert_called_once_with()
    assert provider._quote_ctx.request_history_kline.call_count == (blocked_at == "sdk")

    workers.release.set()
    _join_workers(workers)
    bars = provider.get_kline("US.AAPL", autype="NONE", latency_sensitive=True)
    assert len(bars) == 12 and bars[-1]["close"] == 100.0
    assert len(workers.threads) == 1
    assert not provider._kline_refresh_lock.locked()
    provider._ensure_connected.assert_called_once_with()
    provider._wait_kline_quota.assert_called_once_with("US.AAPL")
    provider._quote_ctx.request_history_kline.assert_called_once()


def test_concurrent_symbols_share_one_refresh_slot(provider, workers):
    start = threading.Event()
    entered = threading.Event()
    returned = []
    callers = []

    def request(**kwargs):
        entered.set()
        assert workers.release.wait(timeout=5)
        return 0, pd.DataFrame(_rows()), ""

    def call(symbol):
        start.wait()
        returned.append(provider.get_kline(symbol, autype="NONE", latency_sensitive=True))

    provider._quote_ctx.request_history_kline.side_effect = request
    try:
        for i in range(12):
            thread = threading.Thread(target=call, args=(f"US.TEST{i}",), daemon=True)
            callers.append(thread)
            thread.start()
        start.set()
        for thread in callers:
            thread.join(timeout=0.5)
        assert not any(t.is_alive() for t in callers)
        assert entered.wait(timeout=1)
        assert returned == [None] * 12
        assert len(workers.threads) == 1
        provider._quote_ctx.request_history_kline.assert_called_once()
    finally:
        start.set()
        workers.release.set()
        for thread in callers:
            thread.join(timeout=2)
        assert not any(t.is_alive() for t in callers)


@pytest.mark.parametrize("failure", ["ret", "exception"])
def test_failure_sets_sick_window_and_expiry_recovers(provider, workers, failure):
    sdk = provider._quote_ctx.request_history_kline
    if failure == "ret":
        sdk.return_value = (-1, None, "mock failure")
    else:
        sdk.side_effect = RuntimeError("mock SDK failure")
    before = time.time()
    assert not provider.kline_sick()
    assert provider.get_kline("US.AAPL", autype="NONE", latency_sensitive=True) is None
    _join_workers(workers)
    assert provider.kline_sick()
    assert provider._kline_sick_until >= before + _KLINE_SICK_WINDOW_SEC
    assert not provider._kline_refresh_lock.locked()
    for i in range(10):
        assert provider.get_kline(f"US.TEST{i}", latency_sensitive=True) is None
    assert len(workers.threads) == 1
    provider._ensure_connected.assert_called_once()
    sdk.assert_called_once()

    provider._kline_sick_until = time.time() - 1
    sdk.side_effect = None
    sdk.return_value = (0, pd.DataFrame(_rows()), "")
    assert provider.get_kline("US.AAPL", autype="NONE", latency_sensitive=True) is None
    _join_workers(workers)
    assert not provider.kline_sick()
    assert len(workers.threads) == 2
    assert provider.get_kline("US.AAPL", autype="NONE", latency_sensitive=True)
    assert sdk.call_count == 2


@pytest.mark.parametrize("failure", ["false", "exception"])
def test_worker_connection_failure_releases_guard(provider, workers, failure):
    if failure == "false":
        provider._ensure_connected.return_value = False
    else:
        provider._ensure_connected.side_effect = RuntimeError("mock connect failure")
    assert provider.get_kline("US.AAPL", latency_sensitive=True) is None
    _join_workers(workers)
    assert not provider._kline_refresh_lock.locked()
    assert provider.kline_sick()
    provider._quote_ctx.request_history_kline.assert_not_called()

    provider._kline_sick_until = time.time() - 1
    provider._ensure_connected.side_effect = None
    provider._ensure_connected.return_value = True
    assert provider.get_kline("US.AAPL", latency_sensitive=True) is None
    _join_workers(workers)
    assert len(workers.threads) == 2
    assert not provider.kline_sick()
    assert provider.get_kline("US.AAPL", latency_sensitive=True)


@pytest.mark.parametrize("failure_at", ["constructor", "start"])
def test_worker_start_failure_releases_guard(provider, workers, monkeypatch, failure_at):
    with monkeypatch.context() as patch:
        broken = Mock()
        if failure_at == "constructor":
            broken.side_effect = RuntimeError("cannot create thread")
        else:
            broken.return_value.start.side_effect = RuntimeError("cannot start thread")
        patch.setattr(provider_module.threading, "Thread", broken)
        assert provider.get_kline("US.AAPL", latency_sensitive=True) is None
        assert not provider._kline_refresh_lock.locked()
        provider._ensure_connected.assert_not_called()
    assert provider.get_kline("US.AAPL", latency_sensitive=True) is None
    _join_workers(workers)
    assert len(workers.threads) == 1
    assert provider.get_kline("US.AAPL", latency_sensitive=True)


def test_background_reconnect_is_not_in_calling_thread(provider, workers):
    entered = threading.Event()
    reconnect_threads = []
    provider._kline_fail_count = 2
    provider._kline_last_reconnect = 0.0
    provider._quote_ctx.request_history_kline.return_value = (-1, None, "failure")

    def reconnect():
        reconnect_threads.append(threading.current_thread())
        entered.set()
        assert workers.release.wait(timeout=5)

    provider.force_reconnect.side_effect = reconnect
    start = time.monotonic()
    assert provider.get_kline("US.AAPL", latency_sensitive=True) is None
    assert time.monotonic() - start < 0.5
    assert entered.wait(timeout=1)
    assert reconnect_threads == workers.threads
    assert provider._kline_refresh_lock.locked()
    assert provider.kline_sick()
    assert provider.get_kline("US.MSFT", latency_sensitive=True) is None
    assert len(workers.threads) == 1


def test_default_path_unchanged_counts_failures_and_reconnects(provider, workers):
    sdk = provider._quote_ctx.request_history_kline
    sdk.return_value = (-1, None, "failure")
    reconnect_threads = []
    provider.force_reconnect.side_effect = lambda: reconnect_threads.append(threading.current_thread())
    for _ in range(3):
        assert provider.get_kline("US.AAPL", autype="NONE") is None
    assert provider.kline_sick()
    provider.force_reconnect.assert_called_once_with()
    assert reconnect_threads == [threading.current_thread()]
    assert provider._kline_fail_count == 0
    for _ in range(3):
        provider.get_kline("US.AAPL", autype="NONE")
    assert provider._kline_fail_count == 3
    provider.force_reconnect.assert_called_once_with()  # 120s 重连冷却未变
    assert sdk.call_count == 6
    assert not workers.threads


def test_success_clears_sick_window_on_default_path(provider, workers):
    provider._kline_sick_until = time.time() + _KLINE_SICK_WINDOW_SEC
    assert provider.get_kline("US.AAPL", autype="NONE")
    assert not provider.kline_sick()
    provider._quote_ctx.request_history_kline.assert_called_once()
    assert not workers.threads


@pytest.mark.parametrize("sick", [False, True])
@pytest.mark.parametrize("cache_source", ["persistent", "push"])
def test_cache_hit_never_connects_or_starts_worker(provider, workers, sick, cache_source):
    bars = _rows()
    if cache_source == "persistent":
        provider._set_cached_kline("US.AAPL|QFQ|K_1M", bars)
    else:
        provider._push_kline_cache["US.AAPL"] = bars
    provider._kline_sick_until = time.time() + _KLINE_SICK_WINDOW_SEC if sick else 0
    assert provider.get_kline("AAPL", count=3, latency_sensitive=True) == bars[-3:]
    provider._ensure_connected.assert_not_called()
    provider._wait_kline_quota.assert_not_called()
    provider._quote_ctx.request_history_kline.assert_not_called()
    assert not workers.threads


@pytest.mark.parametrize("sick", [False, True])
def test_short_cache_returns_immediately_and_refreshes_only_when_healthy(provider, workers, sick):
    bars = _rows(2)
    provider._set_cached_kline("US.AAPL|NONE|K_1M", bars)
    provider._kline_sick_until = time.time() + _KLINE_SICK_WINDOW_SEC if sick else 0
    assert provider.get_kline("US.AAPL", count=800, autype="NONE", latency_sensitive=True) == bars
    _join_workers(workers)
    assert len(workers.threads) == (0 if sick else 1)
    assert provider._quote_ctx.request_history_kline.call_count == (0 if sick else 1)


def test_expired_cache_is_not_returned(provider, workers):
    provider._kline_data_cache["US.AAPL|NONE|K_1M"] = {
        "data": _rows(price=9), "ts": time.time() - provider._kline_cache_ttl - 1,
    }
    assert provider.get_kline("US.AAPL", autype="NONE", latency_sensitive=True) is None
    _join_workers(workers)
    assert provider.get_kline("US.AAPL", autype="NONE", latency_sensitive=True)[0]["close"] == 100


@pytest.mark.parametrize("lock_name", ["_kline_cache_lock", "_push_kline_cache_lock"])
def test_latency_sensitive_does_not_wait_for_cache_locks(provider, workers, lock_name):
    provider._kline_sick_until = time.time() + _KLINE_SICK_WINDOW_SEC
    lock = getattr(provider, lock_name)
    lock.acquire()
    try:
        start = time.monotonic()
        assert provider.get_kline("US.AAPL", latency_sensitive=True) is None
        assert time.monotonic() - start < 0.5
    finally:
        lock.release()
    assert not workers.threads


@pytest.mark.parametrize("ktype,autype", [
    ("K_1M", "NONE"), ("K_1M", "HFQ"), ("K_DAY", "QFQ"),
    ("K_DAY", "NONE"), ("K_15M", "QFQ"),
])
def test_async_refresh_isolates_period_and_adjustment(provider, workers, ktype, autype):
    push = _rows(price=20)
    provider._push_kline_cache["US.AAPL"] = push
    for period in ("K_1M", "K_DAY", "K_15M"):
        for adjustment in ("QFQ", "NONE", "HFQ"):
            if (period, adjustment) != (ktype, autype):
                provider._set_cached_kline(f"US.AAPL|{adjustment}|{period}", _rows(price=30))
    assert provider.get_kline("US.AAPL", ktype=ktype, autype=autype, latency_sensitive=True) is None
    _join_workers(workers)
    bars = provider.get_kline("US.AAPL", ktype=ktype, autype=autype, latency_sensitive=True)
    assert bars[0]["close"] == 100
    kwargs = provider._quote_ctx.request_history_kline.call_args.kwargs
    assert kwargs["autype"] == getattr(AuType, autype)
    assert kwargs["ktype"] == getattr(KLType, ktype)
    assert kwargs["code"] == "US.AAPL"
    assert provider._push_kline_cache["US.AAPL"] == push
    assert len(workers.threads) == 1


@pytest.mark.parametrize("latency_sensitive", [False, True])
def test_none_refresh_cannot_supply_qfq_push_hit(provider, workers, latency_sensitive):
    push = _rows(price=20)
    provider._push_kline_cache["US.AAPL"] = push
    bars = provider.get_kline("US.AAPL", autype="NONE", latency_sensitive=latency_sensitive)
    if latency_sensitive:
        assert bars is None
    else:
        assert bars[0]["close"] == 100
    _join_workers(workers)
    assert provider.get_kline("US.AAPL", latency_sensitive=True) == push
    provider._quote_ctx.request_history_kline.assert_called_once()
    provider._ensure_connected.assert_called_once()
    assert len(workers.threads) == int(latency_sensitive)


@pytest.mark.parametrize("latency_sensitive", [False, True])
def test_quota_rejection_preserves_short_cache(provider, workers, latency_sensitive):
    short = _rows(2)
    provider._set_cached_kline("US.AAPL|NONE|K_1M", short)
    provider._wait_kline_quota.return_value = False
    assert provider.get_kline("US.AAPL", autype="NONE", latency_sensitive=latency_sensitive) == short
    _join_workers(workers)
    provider._wait_kline_quota.assert_called_once_with("US.AAPL")
    provider._quote_ctx.request_history_kline.assert_not_called()
    assert not provider._kline_refresh_lock.locked()
    assert not provider.kline_sick()


def test_bar_tracker_requests_raw_nonblocking_bars():
    from app.playbook.pipeline import _track_bar_peak_trough

    bars = _rows(price=100)
    get_kline = Mock(return_value=bars)
    meta = {}
    entry = bars[0]["time_key"].replace("09:31", "09:30")
    assert _track_bar_peak_trough(meta, SimpleNamespace(futu=SimpleNamespace(get_kline=get_kline)),
                                  "US.AAPL", entry)
    get_kline.assert_called_once_with("US.AAPL", ktype="K_1M", count=800,
                                      autype="NONE", latency_sensitive=True)
    assert meta["peak_price_bar"] == 101
    assert meta["trough_price_bar"] == 99


def test_bar_tracker_never_retries_legacy_signature():
    from app.playbook.pipeline import _track_bar_peak_trough

    def legacy(symbol, ktype, count):
        pytest.fail("不能回退同步旧签名")

    get_kline = Mock(side_effect=legacy)
    meta = {}
    assert not _track_bar_peak_trough(
        meta, SimpleNamespace(futu=SimpleNamespace(get_kline=get_kline)),
        "US.AAPL", "2026-10-22T09:30:00",
    )
    assert get_kline.call_count == 1
    assert meta == {}


@pytest.mark.parametrize("mode", ["cache", "type_error", "blocked_sdk"])
def test_high_atr_shadow_uses_raw_nonblocking_bars(provider, workers, monkeypatch, mode):
    from app.playbook import config, exit_engine
    from app.data import market_data_router

    cfg = SimpleNamespace(get={}.get, get_horizon_top=lambda key, default: default,
                          get_horizon_params=lambda horizon: {})
    monkeypatch.setattr(config, "get_playbook_config", lambda: cfg)
    monkeypatch.setattr(exit_engine, "_compute_dynamic_effective_sl", lambda *args: 95)
    monkeypatch.setattr(exit_engine, "_is_overnight_position", lambda value: False)
    writes = mock_open()
    monkeypatch.setattr(exit_engine, "open", writes, raising=False)
    entered = threading.Event()
    if mode == "cache":
        get_kline = Mock(return_value=_rows())
    elif mode == "type_error":
        def legacy(symbol, ktype, count):
            pytest.fail("影子不能回退同步旧签名")
        get_kline = Mock(side_effect=legacy)
    else:
        def request(**kwargs):
            entered.set()
            assert workers.release.wait(timeout=5)
            return 0, pd.DataFrame(_rows()), ""
        provider._quote_ctx.request_history_kline.side_effect = request
        get_kline = Mock(wraps=provider.get_kline)
    router = SimpleNamespace(futu=SimpleNamespace(get_kline=get_kline))
    monkeypatch.setattr(market_data_router, "get_market_data_router", lambda: router)
    entry = _rows()[0]["time_key"].replace("09:31", "09:30")
    start = time.monotonic()
    result = exit_engine._check_playbook_exit_rules(
        "LONG", 100, 99.5, 95, 100, {"_symbol": "US.AAPL", "atr": 5},
        60, 200, 60, entry,
    )
    assert time.monotonic() - start < 0.5
    assert result == ("", 99.5, 100, False)
    get_kline.assert_called_once_with("US.AAPL", ktype="K_1M", count=400,
                                      autype="NONE", latency_sensitive=True)
    if mode == "blocked_sdk":
        assert entered.wait(timeout=1)
        assert len(workers.threads) == 1
    row = json.loads(writes().write.call_args.args[0])
    assert row["checkpoint"] == 45
    assert row["slope_5m"] == (0.0 if mode == "cache" else None)
    assert row["vwap_dev"] == (-0.005 if mode == "cache" else None)
