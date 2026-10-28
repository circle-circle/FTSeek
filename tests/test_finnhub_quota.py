"""Finnhub 免费档配额治理（2026-10-28）回归测试。

覆盖四件事（对应盘前 76 只批次打爆免费档的根因）:
  T1  industry 落盘缓存（30d）—— 静态字段不再逐只每日请求
  T2  批量快照预取 —— N 只逐只 get_market_snapshot → 1 批
  T4  降级链早退/退避 —— 上游限流时不再向下一家连发 5 次
  T5  进程级共享令牌桶 + 429 全局退避 + 逐请求记账

每条测试都钉住"改动前的错误行为会红"，不是"代码存在即通过"。
"""
import time

import pytest

from app.data import finnhub_limiter as fh_quota
from app.data.providers import fundamental_provider as fp_mod
from app.data.providers.fundamental_provider import FundamentalProvider


@pytest.fixture(autouse=True)
def _reset_quota_and_caches(monkeypatch):
    fh_quota._reset_for_tests()
    FundamentalProvider._profile_cache = {}
    FundamentalProvider._profile_cache_loaded = False
    FundamentalProvider._prefetched = {}
    FundamentalProvider._prefetch_ts = 0.0
    yield
    fh_quota._reset_for_tests()


# ── T5: 共享桶按分钟限速 ──────────────────────────────────────────────
def test_bucket_paces_when_exhausted():
    """桶空后必须排队，而不是放行（原来的 per-instance 桶等于额度×实例数）。"""
    b = fh_quota.SharedTokenBucket(capacity=2, refill_interval_sec=0.2)
    assert b.acquire() and b.acquire()
    t0 = time.monotonic()
    assert b.acquire(timeout=2.0) is True
    assert time.monotonic() - t0 >= 0.15, "第 3 次取令牌应立即排队等待补令牌"
    assert b.granted == 3


def test_acquire_times_out_instead_of_hanging():
    b = fh_quota.SharedTokenBucket(capacity=1, refill_interval_sec=5.0)
    assert b.acquire()
    t0 = time.monotonic()
    assert b.acquire(timeout=0.2) is False
    assert time.monotonic() - t0 < 1.0
    assert b.denied == 1


def test_429_triggers_global_backoff(monkeypatch):
    """撞 429 必须冻结补令牌一段时间（原来只有撞墙线程 sleep(5)，其余线程照轰）。"""
    monkeypatch.setattr(fh_quota, "PENALTY_SEC", 0.3)
    b = fh_quota.SharedTokenBucket(capacity=2, refill_interval_sec=0.05)
    b.acquire(); b.acquire()
    b.on_429("stock/profile2")
    assert b.throttled_429 == 1
    t0 = time.monotonic()
    assert b.acquire(timeout=3.0) is True
    assert time.monotonic() - t0 >= 0.25, "惩罚期内不得补令牌"


def test_finnhub_get_429_marks_bucket_and_records(monkeypatch):
    """_get 收到 429：触发全局退避 + 记账（原来只记 get_quote 的成功调用，429 全漏记）。"""
    recorded = []
    monkeypatch.setattr(fp_mod, "fh_quota", fh_quota)
    monkeypatch.setattr(fp_mod.FundamentalProvider, "_finnhub_get", FundamentalProvider._finnhub_get)

    class _Resp:
        status_code = 429
        def raise_for_status(self):
            raise AssertionError("429 不应走到 raise_for_status")

    import requests
    monkeypatch.setattr(fp_mod.requests, "get", lambda *a, **k: _Resp())

    p = FundamentalProvider()
    monkeypatch.setattr(p, "api_key", "test-key")
    assert p._finnhub_get("stock/profile2", {"symbol": "AAPL"}) is None
    assert fh_quota.get_limiter().throttled_429 == 1


# ── T1: industry 落盘缓存 ─────────────────────────────────────────────
def test_profile_cache_survives_new_instance(tmp_path, monkeypatch):
    """industry 是静态字段：第二个 provider 实例不得再发 profile2 请求。"""
    cache = tmp_path / "profile.json"
    monkeypatch.setattr(FundamentalProvider, "_PROFILE_CACHE_PATH", cache)
    calls = []

    p1 = FundamentalProvider()
    monkeypatch.setattr(p1, "_finnhub_get",
                        lambda ep, params, timeout=15: (calls.append(params["symbol"]),
                                                        {"finnhubIndustry": "Tech"})[1])
    assert p1._get_profile("AAPL")["finnhubIndustry"] == "Tech"
    assert calls == ["AAPL"]

    # 模拟进程重启: 新实例 + 类级内存清空，但磁盘缓存还在
    FundamentalProvider._profile_cache = {}
    FundamentalProvider._profile_cache_loaded = False
    p2 = FundamentalProvider()
    monkeypatch.setattr(p2, "_finnhub_get",
                        lambda ep, params, timeout=15: (calls.append(params["symbol"]), None)[1])
    assert p2._get_profile("AAPL")["finnhubIndustry"] == "Tech"
    assert calls == ["AAPL"], "落盘缓存命中后不得再请求 profile2"
    assert cache.exists()


# ── T2: 批量快照预取 ─────────────────────────────────────────────────
class _FakeCtx:
    def __init__(self):
        self.calls = []

    def get_market_snapshot(self, codes):
        import pandas as pd
        self.calls.append(list(codes))
        rows = [{"code": c, "earning_per_share": 1.0, "net_asset": 10.0, "net_profit": 2.0,
                 "pe_ttm_ratio": 20.0, "pe_ratio": 20.0, "pb_ratio": 2.0,
                 "total_market_val": 1e11, "dividend_ttm": 0, "dividend_ratio_ttm": 0,
                 "volume_ratio": 1.2, "highest52weeks_price": 300, "lowest52weeks_price": 100,
                 "bid_ask_ratio": 1.0, "turnover_rate": 0.5, "pre_price": 200,
                 "pre_change_rate": 0.1, "after_price": 0, "after_change_rate": 0,
                 "net_asset_per_share": 5} for c in codes]
        return 0, pd.DataFrame(rows)


class _FakeSvc:
    def __init__(self, ctx):
        self._quote_ctx = ctx

    def connect(self):
        return True


def test_prefetch_turns_n_per_symbol_calls_into_one_batch(monkeypatch, tmp_path):
    """76 只逐只 get_market_snapshot → 1 批；之后 _fetch_futu 不得再发单只请求。"""
    import app.web_dashboard.manual_order_service as mos_mod

    ctx = _FakeCtx()
    monkeypatch.setattr(mos_mod, "get_manual_order_service", lambda: _FakeSvc(ctx))
    monkeypatch.setattr(FundamentalProvider, "_PROFILE_CACHE_PATH", tmp_path / "p.json")

    p = FundamentalProvider()
    monkeypatch.setattr(p, "_get_profile", lambda bare: {"finnhubIndustry": "Tech"})

    symbols = ["US.AAPL", "US.MSFT", "US.BRK-B"]
    assert p.prefetch_snapshots(symbols) == 3
    assert len(ctx.calls) == 1, f"应只发 1 批，实际 {len(ctx.calls)} 次"
    assert ctx.calls[0] == ["US.AAPL", "US.MSFT", "US.BRK.B"], "类股必须转点号写法"

    for s in symbols:
        res = p._fetch_futu(s)
        assert res and res["status"] == "ok"
    assert len(ctx.calls) == 1, "预取命中后不得再逐只取快照"


def test_prefetch_failure_falls_back_to_per_symbol(monkeypatch, tmp_path):
    """预取失败（连接不上）时必须回退逐只，语义不变。"""
    import app.web_dashboard.manual_order_service as mos_mod

    class _NoConn:
        def connect(self):
            return False

    monkeypatch.setattr(mos_mod, "get_manual_order_service", lambda: _NoConn())
    assert FundamentalProvider().prefetch_snapshots(["US.AAPL"]) == 0


# ── T4: 降级链早退，不放大 ───────────────────────────────────────────
def test_finnhub_fallback_stops_after_metric_failure():
    """metric 失败即整只失败：不得再向 recommendation/news/earnings/profile 追加请求。"""
    endpoints = []
    p = FundamentalProvider()

    def _fake_get(ep, params, timeout=15):
        endpoints.append(ep)
        return None

    p._finnhub_get = _fake_get
    p._get_profile = lambda bare: endpoints.append("stock/profile2")
    assert p._fetch_finnhub("US.AAPL") is None
    assert endpoints == ["stock/metric"], f"限流时应早退，实际发了 {endpoints}"


# ── T5 契约: 三个客户端必须共用同一个桶（防未来重构把它又拆开）─────────────
def test_three_clients_share_one_bucket(monkeypatch):
    from app.data import premarket_enrichment as pm
    from app.data.finnhub_provider import FinnhubProvider

    assert FinnhubProvider()._limiter is fh_quota.get_limiter(), \
        "FinnhubProvider 必须用共享桶（每实例桶 = 额度 × 实例数）"

    # SDK 代理: 桶给不出令牌时必须以异常中止（而不是绕过限速直接打 API）
    class _Inner:
        def company_profile2(self, symbol=""):
            raise AssertionError("未取到令牌却发出了请求")

    monkeypatch.setattr(fh_quota, "acquire", lambda n=1, timeout=0: False)
    proxy = pm._QuotaLimitedClient(_Inner())
    with pytest.raises(RuntimeError):
        proxy.company_profile2(symbol="AAPL")


# ── T5 自校准: 服务端响应头压过本地账本（跨进程共 key 唯一真值）──────────
def test_observe_caps_local_tokens_from_server_headers():
    b = fh_quota.SharedTokenBucket(capacity=10, refill_interval_sec=0.01)
    b.observe({"x-ratelimit-remaining": "2", "x-ratelimit-reset": str(time.time() + 30)})
    st = b.stats()
    assert st["srv_remaining"] == 2
    assert st["tokens_left"] <= 2, "服务端只剩 2 ⇒ 本地不得再以为自己有 10"

    # remaining=0 ⇒ 冻结到 reset（另一个进程已经把额度吃光的情形）
    b.observe({"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(time.time() + 2)})
    t0 = time.monotonic()
    assert b.acquire(timeout=6.0) is True
    assert time.monotonic() - t0 >= 1.0, "服务端额度见底时必须等 reset"