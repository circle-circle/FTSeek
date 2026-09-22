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

    def __init__(self, daily_bars, quote_prev_close: float, klines=None, quote_chg: float = -5.41):
        self._bars = daily_bars
        self._qpc = quote_prev_close
        self._klines = klines or []
        self._qchg = quote_chg

    def get_quote(self, symbol):
        return {
            "price": 101.24, "open": 103.0, "high": 103.5, "low": 100.8,
            "prev_close": self._qpc, "volume": 100_000, "turnover": 1e7,
            "change": -5.78, "change_percent": self._qchg,
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


def _make_router(daily_bars, quote_prev_close: float, klines=None, quote_chg: float = -5.41) -> MarketDataRouter:
    """绕过 __init__ (不连 OpenD), 手工装配最小可用路由器."""
    r = MarketDataRouter.__new__(MarketDataRouter)
    r.futu = _FakeFutu(daily_bars, quote_prev_close, klines=klines, quote_chg=quote_chg)
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


# ─────────────────────────────────────────────────────────────────────────────
# RFC-091 / DEB-20260922 §4.4 守卫: change_percent 必须与实时价同相 (2026-09-22)
#
# 背景: 主路径 ts_ctx 由 LV3TechnicalCalculator 从**日K序列**算出 —— change_percent =
#   (df.close[-2] 作分母, result["price"] 作分子) ⇒ 当日盘前/盘中它恒等于**前一交易日的涨跌幅**;
#   而 router 的"用快照价覆盖 K 线价"只换 price、没重算 change_percent ⇒ "实时价 + 昨日涨跌幅"
#   混装 (实证: RIVN 9/21 ctx -2.66% == 9/18 涨跌幅, NOK 9/15 -13.30% == 9/14, RUM 9/14 -2.65%
#   == 9/11, 差 ≤0.003pp)。W7 不变量自 9/11 起每交易日为此报 38 键基线外新增。
# ─────────────────────────────────────────────────────────────────────────────


def _prior_session_move(bars, upto_index: int = -1) -> float:
    """日K序列内"某根bar相对其前一根"的涨跌幅 — 即计算器口径的 change_percent."""
    closes = [float(b["close"]) for b in bars]
    i = upto_index if upto_index >= 0 else len(closes) + upto_index
    prev, cur = closes[i - 1], closes[i]
    return (cur - prev) / prev * 100.0


def test_calculator_change_percent_is_prior_session_move():
    """根因钉: 计算器口径 = 日K序列内相邻两根 bar 的涨跌幅 (不是"相对真昨收的当日涨幅")."""
    from app.data.lv3_technical_calculator import LV3TechnicalCalculator
    bars = _fake_bars(_et_today(), prior_close=107.02)[:-1]      # 序列止于最后完整交易日
    tech = LV3TechnicalCalculator().calculate_from_klines(bars)
    assert tech is not None
    assert tech["change_percent"] == pytest.approx(_prior_session_move(bars), abs=0.01), (
        "计算器的 change_percent = 最后一根bar相对前一根的涨跌幅 ⇒ 次日读它 = 前一交易日涨跌幅")
    assert tech["change_percent"] != pytest.approx(
        (101.24 - 107.02) / 107.02 * 100.0, abs=0.5), "该口径不得恰好等于'实时价 vs 真昨收'的当日涨幅"


def test_change_percent_recomputed_from_live_price_and_true_prev_close():
    """主守卫: ctx.change_percent 必须以 (实时价, 真昨收) 重算, 并留溯源键."""
    today = _et_today()
    r = _make_router(_fake_bars(today, prior_close=107.02), quote_prev_close=999.0,
                     quote_chg=-0.93)                            # 脏值: 旧口径(前一日涨跌幅)典型量级
    ctx = r.get_full_market_context("US.TEST")
    expect = (101.24 - 107.02) / 107.02 * 100.0                  # = -5.401
    assert ctx.get("change_percent") == pytest.approx(expect, abs=0.01), (
        f"ctx.change_percent 应=({ctx.get('price')} - {ctx.get('prev_close')})/prev_close ⇒ {expect:.3f}")
    assert ctx.get("change_percent") != pytest.approx(-0.93, abs=0.5), "脏值不得存活 (反变异)"
    assert ctx.get("change_percent_source") == "router_recomputed_daily_close", "须留溯源键"


def test_change_percent_untouched_when_prev_close_or_price_missing():
    """fail-open: 真昨收不可得 (且 quote 无 prev_close) ⇒ 保留数据路径原值, 不写溯源键, 不造假数.

    注: 此分支保留的是"K线序列口径"原值 (1m 回退序列自身相邻 bar 涨跌幅) — 即旧行为原样保留,
    属已知残余 (真昨收缺失=日K全不可得, 与 RFC-068 的 fail-open 同语义); 关键是不伪装成"已重算".
    """
    r = _make_router([], quote_prev_close=0.0, klines=_fake_1m_klines(), quote_chg=-0.93)
    ctx = r.get_full_market_context("US.TEST")
    assert ctx is not None
    assert ctx.get("change_percent_source") is None, "缺真昨收时不得声称已重算"
    assert ctx.get("change_percent") == pytest.approx(_prior_session_move(_fake_1m_klines()), abs=0.05), (
        "应保留数据路径原值 (1m 回退口径), 既不改数也不留溯源键")
    assert ctx.get("prev_close") in (0, 0.0, None), "该分支无真昨收 ⇒ 不得凭空编一个"
