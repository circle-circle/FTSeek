"""行情口径修复契约 (2026-10-17 全链路审计).

① SPY MA200 必须 ≥200 根日 K — 原实现 ≥50 根即用短窗均值冒充 MA200;
② VIX 拿不到真实值时不得静默写 20.0, regime 也不得用假 VIX 判 HIGH/LOW_VOL。
"""
import pandas as pd

from app.data.market_data_router import MarketDataRouter


class _FakeCtx:
    def __init__(self, closes):
        self._df = pd.DataFrame({"close": closes})

    def request_history_kline(self, **kwargs):
        return 0, self._df, ""


class _FakeFutu:
    def __init__(self, closes):
        self._quote_ctx = _FakeCtx(closes)

    def get_quote(self, symbol):
        return {"price": 500.0}

    def _ensure_connected(self):
        return True


def _router(closes):
    r = object.__new__(MarketDataRouter)
    r.futu = _FakeFutu(closes)
    r._spy_ma200_cache = None
    r._spy_ma200_cache_time = None
    r._spy_ma200_cache_ttl = 1800
    r._spy_ma20_cache = None
    r._spy_ma50_cache = None
    return r


def test_spy_ma200_requires_200_bars():
    """120 根日 K 不足以算 MA200 ⇒ 不产出 (不得用 120 日均值冒充)。"""
    r = _router([400.0 + i for i in range(120)])
    ctx = r.get_spy_context()
    assert "spy_ma200" not in ctx and r._spy_ma200_cache is None
    # 同批数据仍可产出 MA20/MA50 (口径各自独立)
    assert ctx["spy_ma20"] > 0 and ctx["spy_ma50"] > 0


def test_spy_ma200_uses_exact_200_bar_window():
    closes = [400.0 + i for i in range(250)]
    r = _router(closes)
    ctx = r.get_spy_context()
    assert ctx["spy_ma200"] == sum(closes[-200:]) / 200
    assert r._spy_ma200_cache == ctx["spy_ma200"]


def test_market_regime_without_vix_is_neutral():
    """vix 缺失/None ⇒ 跳过 VIX 分支 (不 TypeError, 不用 20.0 假值)。"""
    r = object.__new__(MarketDataRouter)
    assert r._calc_market_regime({"trend": "NEUTRAL"}) == "RANGE"
    assert r._calc_market_regime({"trend": "NEUTRAL", "vix": None}) == "RANGE"


def test_market_regime_uses_real_vix_extremes():
    r = object.__new__(MarketDataRouter)
    assert r._calc_market_regime({"vix": 30.0, "trend": "NEUTRAL"}) == "HIGH_VOL"
    assert r._calc_market_regime({"vix": 10.0, "trend": "NEUTRAL"}) == "LOW_VOL"
