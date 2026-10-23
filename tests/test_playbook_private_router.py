"""playbook 下单路径私有 MarketDataRouter 单例回归 (2026-09-03, DEB-20260903-intraday-chain-audit §五 D3(b))。

病灶: `_execute_via_dual_broker_inner` 两处**每单** new MarketDataRouter() → 每单一条 OpenD
连接 + 一个 30s 健康守护线程; 守护线程 `while True` 无停止位且强引用 self → 实例与其 socket
永不回收 (实测 21:32 重启后 50min 内新建 17 次, 进程 32 socket / 73 线程; 09-02 全日 26 次)。

修法只把 "N 个实例" 收敛成 "1 个实例", 本文件钉住"零口径变更"的三条边界:
  1. 构造参数与原先逐字相同 (全默认) → 仍走快照路径 (真 bid/ask), 不是订阅/push 路径
     (push 路径 bid=ask=last, 换过去等于动滑点尺子 + 报价健康门 + 深跳空降仓);
  2. 每次取价前清该 symbol 的 provider 报价缓存 → 新鲜度与"每单新建 (空缓存)"逐字等价,
     同单内 arrival price → 成交价兜底不会复用 ≤5s 旧价; 且只清该 symbol, 不清别人的缓存;
  3. 单例懒创建线程安全 (并发下单不重复建连), 且下单路径不再有第二处构造点 (棘轮)。

回滚: 删本文件即可, 零生产代码依赖。
"""
import re
import sys
import threading
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

import app.data.market_data_router as mdr  # noqa: E402
import app.playbook.playbook_service as ps  # noqa: E402

_SVC_SRC = (PROJECT_ROOT / "app" / "playbook" / "playbook_service.py").read_text(encoding="utf-8")

# provider 侧报价缓存键 (与生产实现同源: futu_lv3_provider.py:815 / finnhub_provider.py:215 /
# twelvedata_provider.py:271) — 测试里用真键格式, 键名漂移会当场红。
_KEY_FORMATS = ("quote:{sym}", "fh_quote_{sym}", "quote_{sym}")


class _FakeProvider:
    def __init__(self):
        self._cache = {}
        self._cache_time = {}


class _FakeRouter:
    """替身 router: 记录构造参数 + get_quote 调用, 三个 provider 都带真键格式的缓存。"""

    instances = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.calls = []
        self.futu = _FakeProvider()
        self.fh = _FakeProvider()
        self.td = _FakeProvider()
        _FakeRouter.instances.append(self)

    def get_quote(self, symbol):
        self.calls.append(symbol)
        return {"price": 101.5, "bid": 101.4, "ask": 101.6, "prev_close": 100.0,
                "open": 100.2, "data_source": "futu_lv3_snapshot", "symbol": symbol}


@pytest.fixture(autouse=True)
def private_router_env(monkeypatch):
    """每个用例从"尚未创建单例"开始, 结束后复原模块全局 (不污染其它测试)。"""
    _FakeRouter.instances = []
    monkeypatch.setattr(mdr, "MarketDataRouter", _FakeRouter)
    saved = ps._private_router
    ps._private_router = None
    yield _FakeRouter
    ps._private_router = saved


def _prefill(router, symbol, other="US.OTHER"):
    """把本标的与无关标的的报价缓存都塞满 — 用于验证"只清该 symbol"。"""
    for prov in (router.futu, router.fh, router.td):
        for sym in (symbol, other):
            for fmt in _KEY_FORMATS:
                key = fmt.format(sym=sym)
                prov._cache[key] = {"price": 999.0, "stale": True}
                prov._cache_time[key] = 1.0


class TestPrivateRouterSingleton:
    def test_one_instance_across_many_quotes(self, private_router_env):
        """N 次取价只建 1 个 router (原实现 N 次建 N 个 → 连接 + 线程泄漏)。"""
        for _ in range(3):
            assert ps._private_router_quote("US.NVDA")["price"] == 101.5
        assert len(private_router_env.instances) == 1
        assert len(private_router_env.instances[0].calls) == 3

    def test_constructed_with_default_params(self, private_router_env):
        """构造参数逐字同旧的 `MarketDataRouter()` (全默认) → 报价口径不变。"""
        ps._private_router_quote("US.NVDA")
        inst = private_router_env.instances[0]
        assert inst.args == () and inst.kwargs == {}

    def test_concurrent_first_use_builds_once(self, private_router_env):
        """懒创建双检锁: 并发下单 (不同标的) 也只建一条 OpenD 连接。"""
        barrier = threading.Barrier(4)
        results = []

        def _worker(sym):
            barrier.wait()
            results.append(ps._private_router_quote(sym))

        threads = [threading.Thread(target=_worker, args=(f"US.S{i}",)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert len(results) == 4
        assert len(private_router_env.instances) == 1

    def test_quote_cache_evicted_before_each_call(self, private_router_env):
        """每次取价前清本标的报价缓存 → 与"每单新建 (空缓存)"新鲜度等价。"""
        ps._private_router_quote("US.NVDA")
        first = ps._get_private_router()
        _prefill(first, "US.NVDA")
        ps._private_router_quote("US.NVDA")
        for prov in (first.futu, first.fh, first.td):
            for fmt in _KEY_FORMATS:
                assert fmt.format(sym="US.NVDA") not in prov._cache
                assert fmt.format(sym="US.NVDA") not in prov._cache_time

    def test_eviction_is_surgical_not_a_flush(self, private_router_env):
        """只清本标的 — 别处的缓存 (其它 symbol) 不被误伤。"""
        ps._private_router_quote("US.NVDA")
        router = ps._get_private_router()
        _prefill(router, "US.NVDA")
        ps._private_router_quote("US.NVDA")
        for prov in (router.futu, router.fh, router.td):
            for fmt in _KEY_FORMATS:
                assert fmt.format(sym="US.OTHER") in prov._cache

    def test_provider_without_cache_attrs_still_quotes(self, private_router_env, monkeypatch):
        """provider 缺 _cache/_cache_time (或为 None) 时取价不受影响 (fail-open 守卫)。"""
        ps._private_router_quote("US.NVDA")
        router = ps._get_private_router()
        router.fh = object()          # 无 _cache 属性
        router.td = None              # provider 缺失
        del router.futu._cache_time   # 只有 _cache 没有 _cache_time
        router.futu._cache["quote:US.NVDA"] = {"price": 999.0}
        out = ps._private_router_quote("US.NVDA")
        assert out["price"] == 101.5
        assert "quote:US.NVDA" not in router.futu._cache

    def test_quote_failure_propagates_to_call_site_guard(self, private_router_env):
        """取价异常照旧向上抛 — 由下单路径既有 try/except fail-open 处理 (行为同前)。"""
        router = ps._get_private_router()
        router.get_quote = lambda sym: (_ for _ in ()).throw(RuntimeError("OpenD down"))
        with pytest.raises(RuntimeError):
            ps._private_router_quote("US.NVDA")


class TestCallSiteContract:
    def test_no_per_order_construction_remains(self):
        """棘轮: 整个 playbook_service 只允许 1 处构造 (私有单例内), 不得再出现逐单新建。

        用 AST 数真实调用点 (注释里提到 MarketDataRouter() 不算) — 再引入逐单新建当场红。
        """
        import ast
        tree = ast.parse(_SVC_SRC)
        constructions = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and getattr(node.func, "id", getattr(node.func, "attr", "")) == "MarketDataRouter"
        ]
        assert len(constructions) == 1, f"发现 {len(constructions)} 处 MarketDataRouter 构造 (应仅私有单例 1 处)"
        # 唯一那处必须在 _get_private_router 内
        helper = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef) and n.name == "_get_private_router")
        assert any(getattr(x.func, "id", "") == "MarketDataRouter"
                   for x in ast.walk(helper) if isinstance(x, ast.Call))

    def test_only_arrival_price_uses_the_helper(self):
        """2026-10-23 hotfix 后成交价兜底改券商终态累计量价: 只剩 arrival price 一处取价。

        这不是放宽棘轮: 第二处 quote 兜底已被 55e4331de 删除, 用陈旧报价记账比缺价更坏。
        仍然要求构造点唯一 (上方 test_no_per_order_construction_remains)。
        """
        assert _SVC_SRC.count("_private_router_quote(symbol)") == 1
        assert "quote 兜底" not in _SVC_SRC.split("_private_router_quote(symbol)", 2)[-1]

    def test_helper_never_touches_the_shared_push_router(self):
        """私有单例不复用进程级共享 router — 共享实例的标的在 _subscribed_symbols 内,
        get_quote 会走订阅/push 路径 (bid=ask=last 退化) → 换源等于动滑点尺子 (DEB §五 (a))。"""
        helper_src = _SVC_SRC.split("def _get_private_router", 1)[1].split("\ndef ", 1)[0]
        assert "get_market_data_router" not in helper_src
        assert "MarketDataRouter()" in helper_src
