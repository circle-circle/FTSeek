"""RFC-176: 日K线共享缓存按 symbol 单键 + 取数地板, 多种 count 共享一次解析。"""
import time
from types import SimpleNamespace

from app.data.market_data_router import MarketDataRouter


def _bars(n=120):
    return [{"time": f"2026-01-{i % 28 + 1:02d}", "open": 10 + i, "high": 11 + i,
             "low": 9 + i, "close": 10 + i, "volume": 1000, "turnover": 1e4}
            for i in range(n)]


class _FakeFutu:
    def __init__(self, bars=None, raise_exc=False):
        self.calls = []
        self._bars = bars
        self._raise = raise_exc

    def get_daily_kline(self, symbol, count=60):
        self.calls.append((symbol, count))
        if self._raise:
            raise RuntimeError("futu down")
        return self._bars


class _FakeTD:
    enabled = True

    def __init__(self, bars=None):
        self.calls = []
        self._bars = bars

    def get_daily_bars(self, symbol, count=30):
        self.calls.append((symbol, count))
        return self._bars


def _router(futu, td=None):
    r = MarketDataRouter.__new__(MarketDataRouter)
    r.futu = futu
    r.td = td
    r.av = None
    r.fh = None
    r._daily_bars_cache = {}
    r._daily_bars_cache_ttl = 300
    return r


def test_two_counts_share_one_fetch():
    """60 与 30 两种 count 只解析一次 (原实现按 symbol:count 分键 → 两次)。"""
    futu = _FakeFutu(_bars(120))
    r = _router(futu)
    first = r.get_daily_bars("US.NCLH", count=60)
    second = r.get_daily_bars("US.NCLH", count=30)
    assert len(first) == 60 and len(second) == 30
    assert second == first[-30:]
    assert len(futu.calls) == 1, "同标的不同 count 必须共享一次取数"


def test_small_count_first_then_large_refetches_once():
    """先要 10 根(地板 60) → 再要 60 根仍命中; 要 200 根才重新取。"""
    futu = _FakeFutu(_bars(120))
    r = _router(futu)
    assert len(r.get_daily_bars("US.NCLH", count=10)) == 10
    assert futu.calls[0][1] == 60, "取数地板 60:小 count 也要一次取够"
    assert len(r.get_daily_bars("US.NCLH", count=60)) == 60
    assert len(futu.calls) == 1
    assert len(r.get_daily_bars("US.NCLH", count=200)) == 120  # 源只有 120 根
    assert len(futu.calls) == 2


def test_futu_failure_falls_back_and_caches_symbol_key():
    td = _FakeTD(_bars(90))
    r = _router(_FakeFutu(raise_exc=True), td)
    bars = r.get_daily_bars("US.ILPT", count=60)
    assert len(bars) == 60
    assert td.calls == [("US.ILPT", 60)]
    assert "US.ILPT" in r._daily_bars_cache
    # 第二次同标的其它 count 命中缓存, 不再打 fallback
    assert len(r.get_daily_bars("US.ILPT", count=30)) == 30
    assert len(td.calls) == 1


def test_empty_result_not_cached():
    r = _router(_FakeFutu(_bars(0)))
    assert r.get_daily_bars("US.MNRO", count=60) is None
    assert "US.MNRO" not in r._daily_bars_cache


def test_expired_cache_refetches():
    futu = _FakeFutu(_bars(120))
    r = _router(futu)
    r.get_daily_bars("US.KO", count=60)
    r._daily_bars_cache["US.KO"] = (r._daily_bars_cache["US.KO"][0], time.time() - 301)
    r.get_daily_bars("US.KO", count=60)
    assert len(futu.calls) == 2