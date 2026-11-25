import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.data import futu_lv3_provider as module


@pytest.fixture
def provider(monkeypatch):
    p = module.FutuLV3Provider.__new__(module.FutuLV3Provider)
    p.host, p.port = "127.0.0.1", 11111
    p._quote_ctx = None
    p._connected = False
    p._lock = threading.Lock()
    p._fail_count = 0
    p._setup_push_handlers = Mock()
    p._start_health_watchdog = Mock()
    p._is_us_market_open = Mock(return_value=True)
    p._is_us_pre_market = Mock(return_value=False)
    sock = Mock()
    sock.connect_ex.return_value = 0
    monkeypatch.setattr(module.socket, "socket", Mock(return_value=sock))
    return p


@pytest.fixture
def sdk(monkeypatch, provider):
    import futu

    events = []
    ctx = Mock()
    ctx.set_sync_query_connect_timeout.side_effect = lambda timeout: events.append(("timeout", timeout))

    def ready():
        assert provider._quote_ctx is None
        assert not provider._connected
        events.append(("ready", None))
        return 0, {"connected": True}

    ctx.get_global_state.side_effect = ready
    ctx.get_market_snapshot.return_value = (-1, None)

    def construct(**kwargs):
        assert kwargs == {"host": provider.host, "port": provider.port, "is_async_connect": True}
        events.append(("construct", kwargs))
        return ctx

    constructor = Mock(side_effect=construct)
    monkeypatch.setattr(futu, "OpenQuoteContext", constructor)
    monkeypatch.setattr(futu.SysConfig, "enable_proto_encrypt", Mock())
    return SimpleNamespace(ctx=ctx, constructor=constructor, events=events)


@pytest.mark.parametrize("historical", [False, True])
def test_async_deadline_precedes_ready_and_publication(provider, sdk, historical):
    connect = provider._connect_for_historical if historical else provider._ensure_connected
    assert connect()
    assert [event[0] for event in sdk.events] == ["construct", "timeout", "ready"]
    assert sdk.events[1][1] == module._QUOTE_READY_TIMEOUT_SEC > 0
    assert provider._quote_ctx is sdk.ctx
    assert provider._connected
    if historical:
        sdk.ctx.get_market_snapshot.assert_not_called()
    else:
        sdk.ctx.get_market_snapshot.assert_called_once_with(["US.AAPL"])
        provider._setup_push_handlers.assert_called_once()
    sdk.ctx.close.assert_not_called()


@pytest.mark.parametrize("historical", [False, True])
def test_failed_ready_never_publishes_candidate(provider, sdk, historical):
    sdk.ctx.get_global_state.side_effect = None
    sdk.ctx.get_global_state.return_value = (-1, "Connect timeout")
    connect = provider._connect_for_historical if historical else provider._ensure_connected
    assert not connect()
    assert provider._quote_ctx is None
    assert not provider._connected
    sdk.ctx.close.assert_called_once()
    sdk.ctx.get_market_snapshot.assert_not_called()
    provider._setup_push_handlers.assert_not_called()


def test_deadline_setup_failure_closes_candidate(provider, sdk):
    sdk.ctx.set_sync_query_connect_timeout.side_effect = RuntimeError("deadline failed")
    with pytest.raises(RuntimeError, match="deadline failed"):
        provider._open_ready_quote_context()
    sdk.ctx.get_global_state.assert_not_called()
    sdk.ctx.close.assert_called_once()
    assert provider._quote_ctx is None


def test_candidate_close_error_does_not_hide_ready_error(provider, sdk, caplog):
    sdk.ctx.get_global_state.side_effect = None
    sdk.ctx.get_global_state.return_value = (-1, "Connect timeout")
    sdk.ctx.close.side_effect = RuntimeError("close failed")
    with pytest.raises(RuntimeError, match="Connect timeout"):
        provider._open_ready_quote_context()
    assert "未就绪候选连接关闭失败" in caplog.text


def test_historical_and_regular_creation_share_lock(provider, sdk):
    entered, release = threading.Event(), threading.Event()
    results = []
    workers = []

    def ready():
        entered.set()
        assert release.wait(timeout=3)
        return 0, {}

    sdk.ctx.get_global_state.side_effect = ready
    try:
        first = threading.Thread(target=lambda: results.append(provider._connect_for_historical()))
        second = threading.Thread(target=lambda: results.append(provider._ensure_connected()))
        workers.extend([first, second])
        first.start()
        assert entered.wait(timeout=1)
        second.start()
        assert sdk.constructor.call_count == 1
        release.set()
        for thread in workers:
            thread.join(timeout=2)
        assert not any(thread.is_alive() for thread in workers)
        assert results == [True, True]
        sdk.constructor.assert_called_once()
    finally:
        release.set()
        for thread in workers:
            if thread.ident is not None:
                thread.join(timeout=2)
        assert not any(thread.is_alive() for thread in workers)
