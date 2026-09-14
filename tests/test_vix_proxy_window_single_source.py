"""VIX 代理窗口单一真源 + 四消费方一致性钉子 (2026-09-14, 用户裁「修复再提交」)。

病根 (2026-09-14 17:44 ET 实测, 同进程同一时刻两个 VIX):
    "**哪些市场阶段该走 VIXY 实时代理**"这条规则此前在 4 个 VIX 生产者里各写一份,
    且分成两派 —— `market_data_router` / `market_state` 认 {PRE_MARKET, TRADING},
    而 `alphavantage_provider` / `risk.vix_monitor` 只认 {TRADING}:

      执行门 (market_state → order_executor/playbook_service) + regime 侧   14.71
      risk/vix_monitor → data/vix_cache/vix_data.json (看板 data_service /
                                              day_score / order.thesis /
                                              data_health_check 读)        17.84
    ⇒ 人看到的值 与 系统决策用的值 不是同一个。7/31 事故 (盘前用 FRED 的 7/29
    20.66 生成 23 条指令, 污染 regime; 见 market_data_router.py:955-963 与
    obsidian 2026-08-03_regime_validation) 早已定下方向: 盘前也走代理。

本文件钉住的不变量 (为什么是这四条):
  1. **窗口内容**: `VIXY_PROXY_PHASES == ("PRE_MARKET", "TRADING")` —— 边界本身
     是决策 (盘后/隔夜/周末 FRED 的 T-1 收盘此刻恰是"只欠几小时的真值"),
     改它属消费者可见的行为变更, 须另开辩论; 这里钉住现值使改动必须显式。
  2. **判定语义**: 阶段名 → 布尔, 且 phase 取不到时 **fail-open 为 True**
     (只影响尝试顺序, 代理失败仍回落 FRED ⇒ 无害; fail-closed 反而会在时钟坏时
     静默退回 T-1 陈旧值 —— 本族病灶的共性是"陈旧值被当实时值", 不能反向引入)。
  3. **四消费方一致 (本文件的主角)**: 同一阶段下四个生产者的取值必须**同时**
     由该判据决定 —— 逐个与"判据期望值"比对。修前的分派状态下, 本测试在
     PRE_MARKET 立刻红 (两个值 14.71 / 17.84 并存), 即**这正是今天该有的那道闸**。
  4. **判据唯一性 (源码棘轮)**: 四个生产者都必须调用 `is_vixy_proxy_window`,
     且 `(MarketPhase.TRADING, MarketPhase.PRE_MARKET)` 这一"自带一份"的写法
     不得再出现在 app/ 下任何文件 (vix_source.py 自身除外)。

回滚: 删本文件 + 把四个生产者的窗口判据各自还原为原有的 phase 比较
      (回滚须另开辩论 —— 恢复分派等于让看板值继续与执行门值不一致)。
"""
import ast
import logging
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 两个哨兵值取 2026-09-14 实测 (VIXY $17.3×0.85=14.71 / FRED VIXCLS 17.84):
#   用真实数值而非 1/2 之类的魔数, 是为了让失败信息直接可读 (14.71=代理派 /
#   17.84=日频派), 一眼看出"哪个消费者站哪一派"。
_SENTINEL_PROXY = 14.71
_SENTINEL_FRED = 17.84

# 四个 VIX 生产者 (文件, 定位串) — (定位串用于源码棘轮, 行为侧由 fixture 提供)
_PRODUCER_FILES = (
    "app/analysis/market_state.py",
    "app/data/market_data_router.py",
    "app/data/alphavantage_provider.py",
    "app/risk/vix_monitor.py",
)


def _src(rel):
    return (PROJECT_ROOT / rel).read_text(encoding="utf-8")


class _FakeResp:
    """最小 FRED 响应替身 (market_state 的 FRED 腿只看 status_code / text)。"""

    status_code = 200
    text = f"DATE,VIXCLS\n2026-09-10,{_SENTINEL_FRED}\n"


@pytest.fixture
def _phase(monkeypatch):
    """把系统时钟拨到指定阶段 (四个生产者都经 get_market_phase 取阶段)。"""

    def _set(phase):
        monkeypatch.setattr("app.core.scheduler_triggers.get_market_phase", lambda: phase)
    return _set


@pytest.fixture
def producers(monkeypatch, tmp_path, _phase):
    """四个 VIX 生产者, 一律: 代理腿 → _SENTINEL_PROXY, 日频腿 → _SENTINEL_FRED。

    不触网、不写生产 data/ (vix_monitor 的 CACHE_FILE 重定向到 tmp_path)。
    """
    from app.data import vix_source

    # 全局: 代理腿取数 (market_state 与 router 的代理腿都汇到这里或各自 stub)
    monkeypatch.setattr(vix_source, "fetch_vixy_via_opend", lambda *a, **k: _SENTINEL_PROXY)
    # 全局: 日频腿的 HTTP (只有 market_state 的 FRED 腿走 requests)
    monkeypatch.setattr("requests.get", lambda *a, **k: _FakeResp())

    out = {}

    # ① 执行门 / regime 侧: MarketStateChecker._fetch_vix_data
    from app.analysis.market_state import MarketStateChecker
    _msc = MarketStateChecker()
    out["market_state"] = _msc._fetch_vix_data

    # ② Futu 路由 (regime 侧): MarketDataRouter.get_vix_or_none
    from app.data.market_data_router import MarketDataRouter

    class _StubFutu:
        def get_quote(self, symbol):
            return {"price": 17.3} if symbol == "US.VIXY" else None   # 17.3×0.85=14.71

    class _StubAV:
        def get_vix_or_none(self):
            return _SENTINEL_FRED

    _router = MarketDataRouter.__new__(MarketDataRouter)   # 不建真连接 (只读本地属性)
    _router.futu = _StubFutu()
    _router.av = _StubAV()
    out["market_data_router"] = _router.get_vix_or_none

    # ③ AlphaVantage provider
    from app.data.alphavantage_provider import AlphaVantageProvider
    _av = AlphaVantageProvider(api_key="test-key")
    monkeypatch.setattr(_av, "_fetch_finnhub_vixy", lambda *a, **k: _SENTINEL_PROXY)
    monkeypatch.setattr(_av, "_fetch_fred_vix", lambda *a, **k: _SENTINEL_FRED)
    out["alphavantage_provider"] = _av.get_vix_or_none

    # ④ risk/vix_monitor (看板 vix_cache 的写者)
    from app.risk import vix_monitor as vm
    monkeypatch.setattr(vm, "CACHE_FILE", tmp_path / "vix_data.json")   # 禁写生产 data/
    _vm = vm.VIXMonitor()
    monkeypatch.setattr(_vm, "_fetch_vix_finnhub", lambda: _SENTINEL_PROXY)
    monkeypatch.setattr(_vm, "_fetch_vix_fred", lambda: _SENTINEL_FRED)
    monkeypatch.setattr(_vm, "_fetch_vix_alphavantage", lambda: None)
    monkeypatch.setattr(_vm, "_fetch_vix_fallback", lambda: None)
    out["vix_monitor"] = _vm.get_vix

    return out


# ── 1. 窗口内容与判定语义 (单一真源) ────────────────────────────────────

class TestProxyWindowRule:
    def test_window_content_is_pinned(self):
        """窗口内容 = 决策 (盘后/隔夜/周末维持 FRED 优先); 改动必须显式改这里。"""
        from app.data.vix_source import VIXY_PROXY_PHASES
        assert VIXY_PROXY_PHASES == ("PRE_MARKET", "TRADING")

    @pytest.mark.parametrize("phase_name,expected", [
        ("PRE_MARKET", True),      # VIX 现货不撮合, VIXY 是盘前唯一新鲜值
        ("TRADING", True),
        ("AFTER_HOURS", False),    # 盘后 VIXY 报价稀疏, FRED T-1 收盘只欠几小时=真值
        ("OVERNIGHT", False),
        ("WEEKEND", False),
    ])
    def test_phase_membership(self, phase_name, expected):
        from app.core.scheduler_triggers import MarketPhase
        from app.data.vix_source import is_vixy_proxy_window
        assert is_vixy_proxy_window(getattr(MarketPhase, phase_name)) is expected

    def test_accepts_phase_name_string(self):
        """传字符串阶段名也要认 (调用方可能已有名字而非枚举)。"""
        from app.data.vix_source import is_vixy_proxy_window
        assert is_vixy_proxy_window("PRE_MARKET") is True
        assert is_vixy_proxy_window("WEEKEND") is False

    def test_none_phase_resolves_from_clock(self, monkeypatch, _phase):
        from app.core.scheduler_triggers import MarketPhase
        from app.data.vix_source import is_vixy_proxy_window
        _phase(MarketPhase.PRE_MARKET)
        assert is_vixy_proxy_window() is True
        _phase(MarketPhase.OVERNIGHT)
        assert is_vixy_proxy_window() is False

    def test_phase_detection_failure_fails_open_to_proxy(self, monkeypatch, caplog):
        """取不到阶段 → True (只影响尝试顺序, 代理失败仍回落 FRED ⇒ 无害)。

        反向 (fail-closed=False) 会在时钟坏时静默退回 T-1 陈旧值 —— 正是本族
        病灶 (陈旧值被当实时值) 的形态, 故这里钉住 fail-open 方向 + 可见告警。
        """
        from app.data.vix_source import is_vixy_proxy_window

        def _boom():
            raise RuntimeError("clock broken")

        monkeypatch.setattr("app.core.scheduler_triggers.get_market_phase", _boom)
        with caplog.at_level(logging.WARNING, logger="app.data.vix_source"):
            assert is_vixy_proxy_window() is True
        assert any("fail-open" in r.message for r in caplog.records), \
            "阶段判定失败必须可见 (不许静默)"


# ── 2. 四消费方一致性 (本文件主角: 今天该红的那道闸) ────────────────────

class TestCrossConsumerConsistency:
    @pytest.mark.parametrize("phase_name", [
        "PRE_MARKET", "TRADING", "AFTER_HOURS", "OVERNIGHT", "WEEKEND",
    ])
    def test_all_producers_agree_with_shared_rule(self, producers, _phase, phase_name):
        """同一阶段下四个 VIX 生产者的取值必须同时由单一判据决定。

        修前 (两派并存): PRE_MARKET 下 market_state/router 给 14.71 而
        alphavantage_provider/vix_monitor 给 17.84 ⇒ 本断言当场红。
        """
        from app.core.scheduler_triggers import MarketPhase
        from app.data.vix_source import is_vixy_proxy_window

        phase = getattr(MarketPhase, phase_name)
        _phase(phase)
        expected = _SENTINEL_PROXY if is_vixy_proxy_window(phase) else _SENTINEL_FRED

        got = {name: fetch() for name, fetch in producers.items()}
        assert got == dict.fromkeys(producers, expected), (
            f"{phase_name} 下四个 VIX 生产者口径不一致: {got} —— "
            f"期望全部 {expected} (代理派 {_SENTINEL_PROXY} / 日频派 {_SENTINEL_FRED}); "
            "出现两个值即复现 2026-09-14 的口径分裂"
        )

    def test_flipping_shared_rule_flips_all_four(self, producers, monkeypatch, _phase):
        """把单一判据翻成 False, 四个消费者必须**一起**翻到日频派。

        这一条专治"某处自带判据": 自带判据的消费者不受 monkeypatch 影响, 会留在
        代理派 ⇒ 红。修前 vix_monitor 就是这么红的 (它当时认 is_trading)。
        """
        from app.core.scheduler_triggers import MarketPhase

        _phase(MarketPhase.PRE_MARKET)
        monkeypatch.setattr("app.data.vix_source.is_vixy_proxy_window",
                            lambda *a, **k: False)
        got = {name: fetch() for name, fetch in producers.items()}
        assert got == dict.fromkeys(producers, _SENTINEL_FRED), (
            f"仍有消费者自带窗口判据 (未随单一真源翻转): {got}"
        )


# ── 3. 判据唯一性 (源码棘轮) ───────────────────────────────────────────

class TestWindowRuleSingleSource:
    @pytest.mark.parametrize("rel", _PRODUCER_FILES)
    def test_producer_calls_shared_helper(self, rel):
        assert "is_vixy_proxy_window" in _src(rel), \
            f"{rel} 未走单一真源 is_vixy_proxy_window (不得自带阶段判据)"

    def test_no_module_hardcodes_the_phase_pair(self):
        """`(MarketPhase.TRADING, MarketPhase.PRE_MARKET)` 这写法只许留在真源内。"""
        pat = re.compile(r"MarketPhase\.TRADING\s*,\s*MarketPhase\.PRE_MARKET")
        offenders = sorted(
            str(p.relative_to(PROJECT_ROOT))
            for p in (PROJECT_ROOT / "app").rglob("*.py")
            if pat.search(p.read_text(encoding="utf-8", errors="ignore"))
        )
        assert offenders == [], f"下列文件自带了代理窗口判据 (应收敛到 vix_source): {offenders}"

    def test_helper_is_imported_not_reimplemented(self):
        """AST 级: 四生产者体内不得出现 `== MarketPhase.TRADING` 之类的阶段比较。

        例外: alphavantage_provider 的 cache TTL (60s/600s) 仍按 is_trading 分档
        —— 那是"多久允许再打一次 API", 与"哪个源优先"是两件事, 故该函数体内允许
        出现 is_trading 比较; 这里只钉"窗口判据本身必须来自单一真源" (见上一条)。
        """
        for rel in _PRODUCER_FILES:
            tree = ast.parse(_src(rel))
            for node in ast.walk(tree):
                if isinstance(node, ast.Compare) and any(
                    isinstance(c, ast.Attribute) and c.attr in ("TRADING", "PRE_MARKET")
                    for c in node.comparators
                ):
                    allow = (rel.endswith("alphavantage_provider.py")
                             and _enclosing_func(tree, node.lineno) == "get_vix_or_none")
                    assert allow, (
                        f"{rel}:{node.lineno} 仍在对阶段做直接比较 —— "
                        "窗口判据必须走 is_vixy_proxy_window"
                    )


def _enclosing_func(tree, lineno):
    best = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.lineno <= lineno <= (node.end_lineno or node.lineno):
            if best is None or node.lineno > best.lineno:
                best = node
    return best.name if best else None