"""Futu K 线缓存口径契约 (2026-10-17 全链路审计修复).

钉住三个已发生/已存在的静默错数形态:
  ① 持久化缓存 key 缺 ktype ⇒ 60s 内 K_DAY 与 K_1M 互命中, 指标拿到错周期;
  ② HTTP 拉到的 K_DAY 回写 push ring buffer ⇒ 后续 K_1M 消费方吃到日线;
  ③ get_kline(K_DAY) 按当日过滤 ⇒ 400 天日线只剩 0-1 根 (消费方恒 None)。
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from app.data.futu_lv3_provider import FutuLV3Provider


def _et_dates():
    """与 provider 同口径的 today / 前一交易日 (ET) — 日内 K 线过滤器只认这两天。"""
    now = datetime.now(ZoneInfo("US/Eastern"))
    back = 3 if now.weekday() == 0 else (2 if now.weekday() == 6 else 1)
    return now.strftime("%Y-%m-%d"), (now - timedelta(days=back)).strftime("%Y-%m-%d")


def _bar(day: str, close: float) -> dict:
    return {"time_key": f"{day} 00:00:00", "open": close, "high": close,
            "low": close, "close": close, "volume": 1.0, "turnover": 1.0}


class _FakeQuoteCtx:
    def __init__(self, by_ktype):
        self._by_ktype = by_ktype
        self.calls = []

    def request_history_kline(self, **kwargs):
        self.calls.append(kwargs)
        ktype = str(getattr(kwargs.get("ktype"), "name", kwargs.get("ktype")))
        rows = self._by_ktype.get(ktype, [])
        return 0, pd.DataFrame(rows), ""


def _provider(by_ktype):
    p = FutuLV3Provider()
    p._ensure_connected = lambda: True
    p._wait_kline_quota = lambda symbol: True
    p._quote_ctx = _FakeQuoteCtx(by_ktype)
    return p


def _daily_rows(n=250):
    days = pd.bdate_range(end="2026-10-16", periods=n)
    return [_bar(d.strftime("%Y-%m-%d"), 100.0 + i) for i, d in enumerate(days)]


def _minute_rows(n=12, day=None):
    day = day or _et_dates()[0]
    return [{"time_key": f"{day} 09:{30 + i:02d}:00", "open": 200.0 + i,
             "high": 200.0 + i, "low": 200.0 + i, "close": 200.0 + i,
             "volume": 10.0, "turnover": 100.0} for i in range(n)]


def _t(bar) -> str:
    """HTTP 路径 bar 用 time, push 路径用 time_key (消费方两种都认)。"""
    return str(bar.get("time") or bar.get("time_key") or "")


def test_k_day_returns_full_history_not_only_today():
    """③ K_DAY 请求返回最近 count 根历史日线 (原实现按当日过滤 ⇒ 恒 None)。"""
    p = _provider({"K_DAY": _daily_rows(250)})
    bars = p.get_kline("US.SPY", ktype="K_DAY", count=20)
    assert bars is not None and len(bars) == 20
    assert _t(bars[-1]).startswith("2026-10-16")
    assert _t(bars[0]) < _t(bars[-1])


def test_k_day_does_not_pollute_push_kline_cache():
    """② K_DAY HTTP 结果不得写入 1M push ring buffer。"""
    p = _provider({"K_DAY": _daily_rows(250)})
    p.get_kline("US.SPY", ktype="K_DAY", count=20)
    assert p._push_kline_cache == {}


def test_ktype_cache_keys_are_isolated():
    """① 同一 symbol 的 K_DAY / K_1M 持久化缓存互不命中。"""
    p = _provider({"K_DAY": _daily_rows(250), "K_1M": _minute_rows(30)})
    day_bars = p.get_kline("US.AAPL", ktype="K_DAY", count=20)
    min_bars = p.get_kline("US.AAPL", ktype="K_1M", count=20)
    assert len(day_bars) == 20 and len(min_bars) >= 20
    assert _t(day_bars[-1]).endswith("00:00:00"), "K_DAY 命中了 1M 缓存"
    assert _t(min_bars[-1]).endswith("09:59:00"), "K_1M 命中了日线缓存"
    # 两次都真的走了 HTTP (没有互命中缓存), 且 key 带周期标识
    assert len(p._quote_ctx.calls) == 2
    assert any("K_DAY" in k for k in p._kline_data_cache)
    assert any("K_1M" in k for k in p._kline_data_cache)


def test_push_cache_only_serves_k_1m():
    """② 非 K_1M 请求不得复用 push 的 1 分钟序列。"""
    http_rows = _minute_rows(20)
    for bar in http_rows:
        bar["close"] = 500.0
    p = _provider({"K_15M": http_rows})
    with p._push_kline_cache_lock:
        p._push_kline_cache["US.AAPL"] = _minute_rows(20)
    bars = p.get_kline("US.AAPL", ktype="K_15M", count=10)
    assert bars
    assert bars[0]["close"] == 500.0, "K_15M 复用了 push 的 1M bar"


def test_k_1m_still_uses_push_cache():
    """修复不得误伤正常路径: K_1M 仍优先吃 push ring buffer (近零成本)。"""
    p = _provider({})
    with p._push_kline_cache_lock:
        p._push_kline_cache["US.AAPL"] = _minute_rows(12)
    bars = p.get_kline("US.AAPL", ktype="K_1M", count=10)
    assert len(bars) == 10 and _t(bars[-1]).endswith("09:41:00")
    assert p._quote_ctx.calls == [], "push 缓存命中时不应发 HTTP"
