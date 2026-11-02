"""hasattr 能力探测死门审计 (2026-11-01 全仓扫描 + 4 个公共方法形候选逐一裁决).

背景: `hasattr(obj, '<不存在的方法>')` 的判据恒 False, 守卫块整体降级为空操作;
若外面再包 `except → debug`, 就既没跑过也没有任何告警 (实锤: LLM 逐笔归因, 已拆)。

全仓扫法 (346 处 hasattr → 方法形且全仓无定义 41 处 → 公共方法形 4 处) 的裁决表:

| # | 位置 | 探测名 | 裁决 |
|---|------|--------|------|
| 1 | app/catalyst/reverse_catalyst.py | pipeline.get_recent_items | NewsPipelineV2 无此方法 ⇒ **死分支, 已删**; DB 兜底一直活着 (news_us.db 414 行/当日) |
| 2 | app/core/scheduler_core.py | _adapter.get_stock_names | 全仓无此接口 ⇒ **死探测, 已删**; wl_names 恒空 ⇒ 新闻"按公司名匹配"从未生效 |
| 3 | app/data/providers/futu_extended_provider.py | ctx.get_research_analyst_consensus | **误报 — 保留**: 被探测的是 futu SDK 的 OpenQuoteContext, 已装 10.09.6908 实测有此方法 |
| 4 | app/monitoring/trading_monitor.py | cb.is_open | 真死门 + **死模块** (get_monitor/start_monitor 全仓零调用方) ⇒ 模块整体**已退役 2026-11-02** (340 行删除); 未来做日内监控须新建在有调用方的位置 |

本文件钉住 1/2 的拆除与 3/4 的前提, 防"照着旧注释把死门重建一遍"。
"""
import inspect
from pathlib import Path

import pytest

from app.core import scheduler_core as sc
from app.news_v2.pipeline import NewsPipelineV2
from app.risk.market_circuit_breaker import MarketCircuitBreaker
from app.risk.circuit_breaker import SystemCircuitBreaker

REPO = Path(__file__).resolve().parents[1]


def _code_only(src: str) -> str:
    return "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))


def _app_code_sources():
    for p in (REPO / "app").rglob("*.py"):
        if "__pycache__" in str(p):
            continue
        yield p, _code_only(p.read_text(encoding="utf-8", errors="ignore"))


class TestSite1ReverseCatalyst:
    def test_no_phantom_probe(self):
        src = _code_only((REPO / "app/catalyst/reverse_catalyst.py").read_text(encoding="utf-8"))
        assert "get_recent_items" not in src, (
            "NewsPipelineV2 没有 get_recent_items ⇒ hasattr 恒 False, in-memory 新闻读取是死分支; "
            "要恢复先给 NewsPipelineV2 真实现并单测")

    def test_db_fallback_still_present(self):
        """反向: 删死分支不得把真正生效的 DB 兜底一起删掉。"""
        src = (REPO / "app/catalyst/reverse_catalyst.py").read_text(encoding="utf-8")
        assert "us_news" in src and "news_us.db" in src

    def test_pipeline_really_lacks_method(self):
        assert not hasattr(NewsPipelineV2, "get_recent_items")


class TestSite2StockNames:
    def test_no_phantom_probe(self):
        src = _code_only(inspect.getsource(sc))
        assert "get_stock_names" not in src, (
            "全仓无该接口 ⇒ hasattr 恒 False, wl_names 恒空; 要先在 BrokerAdapter 实现再接线")

    def test_no_adapter_implements_it(self):
        hits = [str(p.relative_to(REPO)) for p, src in _app_code_sources()
                if "def get_stock_names" in src]
        assert not hits, f"已有适配器实现 get_stock_names ({hits}) ⇒ 拆除前提变了, 需重评接线"


class TestSite3FutuSDKProbeKept:
    def test_probe_targets_external_sdk_and_method_exists(self):
        """被探测的是 futu SDK 的 context, 不是本仓类 ⇒ 全仓无 def 属正常, 不得误删。"""
        from futu import OpenQuoteContext
        assert hasattr(OpenQuoteContext, "get_research_analyst_consensus"), (
            "已装 futu SDK 不再提供该接口 ⇒ provider 的分支恒 failed, 需改为显式降级说明")

    def test_provider_still_guards_with_clear_error(self):
        src = (REPO / "app/data/providers/futu_extended_provider.py").read_text(encoding="utf-8")
        assert "SDK lacks get_research_analyst_consensus" in src


class TestSite4ModuleRetired:
    """4) TradingMonitor 已整体退役 (2026-11-02), 而不是修它的死守卫。

    退役依据: ①get_monitor/start_monitor/stop_monitor 全仓零调用方 (从未启动)
    ②4 项能力全有活替代 (watchdog 心跳 / SystemCircuitBreaker.is_triggered +
    MarketCircuitBreaker.is_trading_suspended / risk.pdt_guard / pre_trade_gate 日亏熔断)
    ③内部 cb.is_open 本身恒 False; ④最后改动 2026-07-27。
    若将来要做日内监控, 必须新建在有真实调用方的位置, 不得复活本模块的失败告警。
    """

    def test_package_gone(self):
        import importlib
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module('app.monitoring')

    def test_no_starter_references_left(self):
        hits = [str(p.relative_to(REPO)) for p, src in _app_code_sources()
                if "start_monitor(" in src or "TradingMonitor" in src]
        assert not hits, f"仍有代码引用已退役的 TradingMonitor: {hits}"

    def test_live_replacements_still_exist(self):
        """反向: 删死模块不得连活在用的替代者一起删 (否则是过度删改)。"""
        from app.risk.market_circuit_breaker import MarketCircuitBreaker
        from app.risk.circuit_breaker import SystemCircuitBreaker
        from app.risk.pdt_guard import PDTGuard
        from app.observability.watchdog import Watchdog
        assert hasattr(MarketCircuitBreaker, "is_trading_suspended")
        assert hasattr(SystemCircuitBreaker, "is_triggered")
        assert callable(PDTGuard.check_trade_permission)
        assert hasattr(Watchdog, "register_heartbeat") or hasattr(Watchdog, "start")

    def test_real_apis_for_future_wiring(self):
        from app.risk.market_circuit_breaker import MarketCircuitBreaker
        assert not hasattr(MarketCircuitBreaker, "is_open"), (
            "is_open 仍不存在 ⇒ 若将来新建监控器, 别再按这个名字写守卫")