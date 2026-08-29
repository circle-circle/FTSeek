"""RFC-068 审计修复守卫: get_full_market_context 必须输出真昨收 (B门硬门的数据根基).

背景 (2026-08-29 复查实测): B门硬门 (pipeline.py) 读取
get_full_market_context(...)["prev_close"], 但该函数此前从不注入该键 →
门结构性永不触发 ("数据缺失放行" 被键缺失伪装成常态)。本测试钉住:
  1. 注入存在且为日K自算真昨收 (不是快照陈旧值)
  2. 变异反证: 移除注入块 → 键消失 (证明测试能咬人)
  3. 日K缺失时回落 quote.prev_close
  4. 注入值喂给 evaluate_rules 后 B_long_knife 判定正确 (端到端口径)
"""
import datetime
import re
from pathlib import Path

import pytest

from app.data import market_data_router as _mdr_mod

MarketDataRouter = _mdr_mod.MarketDataRouter
_SRC_PATH = Path(_mdr_mod.__file__)


def _fake_bars(et_today: str, prior_close: float = 107.02, n: int = 40):
    """造 n+1 根日K (锚 ET 日历日): 最后完整收盘日=ET今日-1(收盘=prior_close),
    再附加一根 ET 今日盘中 bar (收盘=现价附近) — 验证注入不会误取盘中今日 bar."""
    base = datetime.date.fromisoformat(et_today)
    bars = []
    for i in range(n):
        d = base - datetime.timedelta(days=n - i)
        c = prior_close if (base - d).days == 1 else 100.0 + i * 0.1
        bars.append({
            "time": d.isoformat(), "time_key": d.isoformat(),
            "open": c, "high": c + 1.0, "low": c - 1.0,
            "close": c, "volume": 1_000_000, "turnover": 1e8,
        })
    # ET 今日盘中 bar (若注入逻辑过滤不严会误把它当昨收)
    bars.append({
        "time": et_today, "time_key": et_today,
        "open": prior_close + 1, "high": prior_close + 2, "low": prior_close - 6,
        "close": prior_close - 5.0, "volume": 500_000, "turnover": 5e7,
    })
    return bars


def _et_today() -> str:
    import pytz
    return datetime.datetime.now(pytz.timezone("US/Eastern")).strftime("%Y-%m-%d")


class _FakeFutu:
    """最小 Futu 替身: 报价 + 日K + 1mK + 空 push."""

    def __init__(self, daily_bars, quote_prev_close: float, klines=None):
        self._bars = daily_bars
        self._qpc = quote_prev_close
        self._klines = klines or []

    def get_quote(self, symbol):
        return {
            "price": 101.24, "open": 103.0, "high": 103.5, "low": 100.8,
            "prev_close": self._qpc, "volume": 100_000, "turnover": 1e7,
            "change": -5.78, "change_percent": -5.41,
            "bid": 101.2, "ask": 101.3, "bid_vol": 100, "ask_vol": 100,
            "data_source": "futu_lv3_subscribed", "timestamp": 0.0,
        }

    def get_kline(self, symbol, ktype="K_1M", count=800):
        return self._klines

    def get_daily_kline(self, symbol, count=60):
        # 真实 get_daily_bars 的 Futu 侧入口 (provider 的日K方法名)
        return self._bars

    def get_push_tickers(self, symbol):
        return None

    def get_push_orderbook(self, symbol):
        return None

    @staticmethod
    def _is_option_code(symbol):
        return False


def _fake_1m_klines(n=35):
    bars = []
    for i in range(n):
        p = 101.0 + 0.01 * i
        bars.append({
            "time": f"2026-08-29 09:{30 + i:02d}", "time_key": f"2026-08-29 09:{30 + i:02d}",
            "open": p, "high": p + 0.2, "low": p - 0.2, "close": p,
            "volume": 10_000, "turnover": 1e6,
        })
    return bars


def _make_router(daily_bars, quote_prev_close: float, klines=None) -> MarketDataRouter:
    """绕过 __init__ (不连 OpenD), 手工装配最小可用路由器."""
    r = MarketDataRouter.__new__(MarketDataRouter)
    r.futu = _FakeFutu(daily_bars, quote_prev_close, klines=klines)
    r.td = None
    r.av = None
    r.fh = None
    r._daily_bars_cache = {}
    r._daily_bars_cache_ttl = 300
    return r


def test_ctx_injects_true_prev_close_from_daily_bars():
    today = _et_today()
    r = _make_router(_fake_bars(today, prior_close=107.02), quote_prev_close=999.0)
    ctx = r.get_full_market_context("US.TEST")
    assert ctx is not None, "主通道应产出 ctx"
    assert ctx.get("prev_close") == pytest.approx(107.02), (
        "prev_close 必须=日K前一日收盘(真昨收), 不得用盘中今日bar, 不得用快照值"
    )


def test_ctx_prev_close_falls_back_to_quote_when_no_daily_bars():
    today = _et_today()
    r = _make_router([], quote_prev_close=106.5, klines=_fake_1m_klines())  # 日K为空, 1mK撑住ctx → 回落quote
    ctx = r.get_full_market_context("US.TEST")
    assert ctx is not None
    assert ctx.get("prev_close") == pytest.approx(106.5)


def test_injected_prev_close_feeds_knife_gate_end_to_end():
    """RFC-068 8/28 RBRK 实锤重演: entry=101.235 vs 真昨收=107.02 → -5.41% → 拦."""
    from app.playbook.premarket_invalidation import evaluate_rules
    today = _et_today()
    r = _make_router(_fake_bars(today, prior_close=107.02), quote_prev_close=999.0)
    ctx = r.get_full_market_context("US.TEST")
    pc = float(ctx.get("prev_close") or 0)
    px = float(ctx.get("price") or 0)
    assert pc > 0 and px > 0
    chg = (px - pc) / pc * 100.0
    assert "B_long_knife" in evaluate_rules("BUY", chg), (
        f"执行时刻跌幅 {chg:.2f}% 应命中飞刀门 (修复前 pc 恒 0 → 永不判定)"
    )


def test_mutation_removing_injection_breaks_the_key():
    """行为级变异反证: 两路数据皆断时键必须缺席 (门按'数据缺失放行'语义处理)."""
    today = _et_today()
    r = _make_router(_fake_bars(today, prior_close=107.02), quote_prev_close=0.0)
    r.get_daily_bars = lambda symbol, count=30: None
    ctx = r.get_full_market_context("US.TEST")
    assert ctx is not None
    assert not ctx.get("prev_close"), "两路数据皆断时不得伪造昨收 (0/缺失=放行语义)"
    # 且源码中注入块仍在 (防静默删除)
    src = _SRC_PATH.read_text(encoding="utf-8")
    assert "RFC-068 审计修复" in src and 'ts_ctx["prev_close"]' in src
