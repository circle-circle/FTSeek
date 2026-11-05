"""订阅类型升级 (2026-11-06, RFC-158 附录) — 契约测试。

病灶: 两层订阅台账都只记「symbol」不记「type」, 于是「先按精简集 (QUOTE+K_1M) 订过、
后要求 ORDER_BOOK/TICKER」的类型升级被 `already_subscribed` 静默吞掉 ⇒ 盘口推送从未订阅 ⇒
market_data_router 以 last_price 顶 bid/ask (book_is_proxy=True) ⇒ entry_confirm 的 mid
尺子逐笔回落 legacy 混尺 (11-05 重开首日 4/4 arm 全 legacy)。

这两个用例在修复前必须红 (变异验证: 旧实现第二次 subscribe 直接返回, 一次 ctx 调用都不发)。

⚠ 用 `__new__` 绕过 provider `__init__`: 后者会建连接/初始化大量缓存, 单测里不需要也不该碰;
   这里只装配 subscribe() 真正读到的属性。
"""

import sys
import threading
import types

import pytest

import app.data.futu_lv3_provider as prov_mod
from app.data.futu_lv3_provider import FutuLV3Provider
from app.data.subscription_manager import SubscriptionManager


_ALL_TYPES = ("QUOTE", "K_1M", "ORDER_BOOK", "TICKER")


class _FakeQuoteCtx:
    """记录每一次 subscribe(symbols, [type]) 调用; fail_types 里的类型返回 ret!=0。"""

    def __init__(self, fail_types=()):
        self.calls = []
        self.fail_types = set(fail_types)

    def subscribe(self, symbols, sub_types, subscribe_push=True):
        name = str(sub_types[0])
        self.calls.append((name, list(symbols)))
        if name in self.fail_types:
            return -1, "subscribe quota/limit"
        return 0, "ok"


@pytest.fixture(autouse=True)
def _stub_futu_symbol_map(monkeypatch):
    """把 SubType 映射换成纯字符串, 断言只看类型名 (不依赖 SDK 常量身份)。"""
    monkeypatch.setattr(prov_mod, "_futu_sub_type_map",
                        lambda: {t: t for t in _ALL_TYPES})
    monkeypatch.setattr(prov_mod, "_configured_stock_sub_types",
                        lambda: ["QUOTE", "K_1M"])


def _provider(ctx):
    p = FutuLV3Provider.__new__(FutuLV3Provider)   # 见 docstring: 绕过 __init__
    p._quote_ctx = ctx
    p._connected = True
    p._sub_lock = threading.Lock()
    p._subscribed_symbols = set()
    p._sub_types_by_symbol = {}
    p._failed_sub_types = {}
    p._ensure_connected = lambda: True
    return p


# ══════════════════════════════════════════════════════════════════
# provider 层
# ══════════════════════════════════════════════════════════════════
def test_provider_upgrades_missing_types_only():
    """先精简集、后要盘口 ⇒ 只补 ORDER_BOOK/TICKER, 不重订 QUOTE/K_1M。"""
    ctx = _FakeQuoteCtx()
    p = _provider(ctx)

    p.subscribe(["US.AAPL"], sub_types=["QUOTE", "K_1M"])
    assert [c[0] for c in ctx.calls] == ["QUOTE", "K_1M"]

    ok = p.subscribe(["US.AAPL"], sub_types=list(_ALL_TYPES))
    assert ok is True
    assert [c[0] for c in ctx.calls[2:]] == ["ORDER_BOOK", "TICKER"]
    assert all(c[1] == ["US.AAPL"] for c in ctx.calls[2:])
    assert p._sub_types_by_symbol["US.AAPL"] == set(_ALL_TYPES)

    # 类型齐了 ⇒ 再来一次是真空操作 (不能每 60s 重订一遍)
    n = len(ctx.calls)
    p.subscribe(["US.AAPL"], sub_types=list(_ALL_TYPES))
    assert len(ctx.calls) == n


def test_provider_retries_failed_upgrade_type():
    """补订失败的类型留痕, 下一轮自动重试 (原实现只重试"曾失败"的类型, 这条要保住)。"""
    ctx = _FakeQuoteCtx(fail_types={"ORDER_BOOK"})
    p = _provider(ctx)

    p.subscribe(["US.AAPL"], sub_types=["QUOTE", "K_1M"])
    p.subscribe(["US.AAPL"], sub_types=list(_ALL_TYPES))
    assert p._failed_sub_types["US.AAPL"] == {"ORDER_BOOK"}
    assert "ORDER_BOOK" not in p._sub_types_by_symbol["US.AAPL"]

    ctx.fail_types.clear()
    n = len(ctx.calls)
    p.subscribe(["US.AAPL"], sub_types=list(_ALL_TYPES))
    retried = [c[0] for c in ctx.calls[n:]]
    assert retried == ["ORDER_BOOK"], retried
    assert "ORDER_BOOK" in p._sub_types_by_symbol["US.AAPL"]
    assert not p._failed_sub_types.get("US.AAPL")


# ══════════════════════════════════════════════════════════════════
# manager 层 (同一病灶的上一层: 符号级 _subscribed_codes 曾在这里就短路)
# ══════════════════════════════════════════════════════════════════
class _FakeGateway:
    def __init__(self):
        self.calls = []

    def subscribe(self, codes, sub_types):
        self.calls.append((list(codes), list(sub_types)))
        return True

    def unsubscribe_all(self, codes, sub_types=None):
        return True

    def is_healthy(self):
        return True

    def force_reconnect(self):
        return True


def _manager(gw):
    m = SubscriptionManager(gw)
    m._min_action_interval = 0        # 关掉 30s 防抖 (测试里要连续调)
    return m


def test_manager_forwards_already_subscribed_codes_to_provider():
    """已在 _subscribed_codes 里的 code 也必须转发 —— 类型升级只能由 provider 判。

    修前 (符号级短路): 第二次调 ensure_subscribed 直接 return, gateway 一次也收不到。
    """
    gw = _FakeGateway()
    m = _manager(gw)

    r1 = m.ensure_subscribed(["AAPL"], sub_types=["QUOTE", "K_1M"])
    assert r1["new_subs"] == 1 and r1["already_subscribed"] == 0
    assert gw.calls[-1] == (["US.AAPL"], ["QUOTE", "K_1M"])

    r2 = m.ensure_subscribed(["AAPL"], sub_types=list(_ALL_TYPES))
    assert r2["new_subs"] == 0 and r2["already_subscribed"] == 1
    assert gw.calls[-1] == (["US.AAPL"], list(_ALL_TYPES)), \
        "已订阅 code 被短路 ⇒ provider 永远拿不到补订请求"


def test_manager_keeps_ledger_small_and_counts_new_only_once():
    gw = _FakeGateway()
    m = _manager(gw)
    m.ensure_subscribed(["AAPL", "MSFT"], sub_types=list(_ALL_TYPES))
    assert m.subscribed_codes == {"US.AAPL", "US.MSFT"}

    # 再次请求同一批: 不重复计入 new_subs, 但请求仍然转发 (provider 幂等)
    r = m.ensure_subscribed(["AAPL", "MSFT"], sub_types=list(_ALL_TYPES))
    assert r["new_subs"] == 0 and r["already_subscribed"] == 2
    assert m.subscribed_codes == {"US.AAPL", "US.MSFT"}