"""P-1 名单合一 + P-2 订阅动态化 (2026-08-01, DEB-20260724 §7) 回归测试.

P-1: watchlist 块为唯一真源, topup/订阅/基准按 roles 派生 — 锁死派生结果防漂移.
P-2b: _get_subscription_codes 动态集合 = 基准 ∪ 指令 ∪ 持仓 (∪可选静态) ; 回滚路径保真.
"""
from unittest.mock import MagicMock, patch

import pytest

from app.utils.watchlist_manager import get_watchlist_manager

EXPECTED_TOPUP = {
    "US.NVDA", "US.TSLA", "US.AMD", "US.META", "US.AMZN", "US.GOOG", "US.MSFT",
    "US.AAPL", "US.AVGO", "US.TSM", "US.NFLX", "US.SMCI", "US.PLTR", "US.COIN",
    "US.MU", "US.QCOM", "US.ARM", "US.MRVL", "US.UBER", "US.RIVN", "US.SNOW",
    "US.SHOP", "US.DELL", "US.CRWD",
}


class TestRoleRegistry:
    def test_topup_eligible_matches_retired_static_pool(self):
        """静态 pool 退役时的一对一迁移对账 — 24 只不多不少."""
        mgr = get_watchlist_manager()
        assert set(mgr.get_symbols_by_role("topup_eligible")) == EXPECTED_TOPUP

    def test_benchmark_is_spy_qqq(self):
        mgr = get_watchlist_manager()
        assert set(mgr.get_symbols_by_role("benchmark")) == {"US.SPY", "US.QQQ"}

    def test_subscribe_role_equals_enabled_plus_benchmark(self):
        """subscribe 角色 = 44 enabled 股票 + SPY/QQQ (静态回退集合 ≤ 旧行为+基准)."""
        mgr = get_watchlist_manager()
        enabled = {s["code"] for s in mgr.get_all_enabled()}
        assert set(mgr.get_symbols_by_role("subscribe")) == enabled | {"US.SPY", "US.QQQ"}

    def test_disabled_but_topup_eligible_not_filtered(self):
        """NFLX/QCOM enabled=false 但仍 topup_eligible (角色派生不看 enabled)."""
        mgr = get_watchlist_manager()
        topup = set(mgr.get_symbols_by_role("topup_eligible"))
        assert {"US.NFLX", "US.QCOM"} <= topup

    def test_topup_only_symbols_present(self):
        """07-25 扩容的 topup 专属标的 (不在扫描池) 必须迁入真源."""
        mgr = get_watchlist_manager()
        topup = set(mgr.get_symbols_by_role("topup_eligible"))
        assert {"US.SMCI", "US.PLTR", "US.MRVL", "US.SNOW", "US.SHOP",
                "US.DELL", "US.CRWD"} <= topup


def _make_scheduler(dynamic=True, static=False):
    """构造最小可用 scheduler 实例 (绕开 __init__ 重依赖)."""
    from app.core.scheduler_core import TradingScheduler
    sched = TradingScheduler.__new__(TradingScheduler)
    sched.watchlist = ["US.TSLA", "US.NVDA"]
    sched.position_store = None
    return sched, {"subscription": {"dynamic_enabled": dynamic,
                                    "static_watchlist_enabled": static}}


class TestDynamicSubscription:
    def _run(self, dynamic, static, instr=(), opens=()):
        sched, cfg = _make_scheduler(dynamic, static)
        with patch("app.core.config_container.get_config") as gc, \
             patch.object(sched, "_today_instruction_symbols", return_value=set(instr)), \
             patch.object(sched, "_playbook_open_symbols", return_value=set(opens)):
            gc.return_value.get_dotted = lambda k, d=None: cfg.get(k, d)
            return set(sched._get_subscription_codes())

    def test_dynamic_default_set(self):
        """默认: 基准 ∪ 指令 ∪ 持仓, 不含静态 watchlist."""
        codes = self._run(True, False, instr={"US.MU"}, opens={"US.COIN"})
        assert {"US.SPY", "US.QQQ", "US.MU", "US.COIN"} <= codes
        assert "US.TSLA" not in codes  # 静态 watchlist 默认不并入

    def test_static_watchlist_opt_in(self):
        codes = self._run(True, True)
        assert {"US.TSLA", "US.NVDA", "US.SPY", "US.QQQ"} <= codes

    def test_rollback_legacy_path(self):
        """dynamic_enabled=false → 旧行为: 静态 watchlist (∪PositionStore), 无基准/指令."""
        codes = self._run(False, False, instr={"US.MU"}, opens={"US.COIN"})
        assert codes == {"US.TSLA", "US.NVDA"}

    def test_source_failure_degrades_not_raises(self):
        """指令/持仓查询异常 → 降级为基准集合, 不炸订阅循环."""
        sched, cfg = _make_scheduler(True, False)
        with patch("app.core.config_container.get_config") as gc, \
             patch.object(sched, "_today_instruction_symbols", side_effect=RuntimeError("db busy")), \
             patch.object(sched, "_playbook_open_symbols", side_effect=RuntimeError("db busy")):
            gc.return_value.get_dotted = lambda k, d=None: cfg.get(k, d)
            codes = set(sched._get_subscription_codes())
        assert {"US.SPY", "US.QQQ"} <= codes
