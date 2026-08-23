# tests/execution/test_broker_adapter.py
"""FutuBrokerAdapter 契约测试 (1194 行真实下单通道).

B2 (2026-08-24, OKR-2026-09 基建, 体检 M1 收窄版): 原仅 16 行初始化测试.
补齐三条资金路径契约 (全部离线, 不触真实连接):
  1. 名义兜底门禁 _notional_sanity_check: fail-close / 30% 硬帽 / 平仓豁免
  2. submit_order 断连安全: sim→模拟成交, real→拒绝 (不允许断连交易)
  3. _simulate_fill 返回结构契约
"""
import os
from types import SimpleNamespace

import pytest

from app.core.models import Direction, Order, OrderType
from app.execution.broker_adapter import FutuBrokerAdapter


@pytest.fixture
def adapter():
    return FutuBrokerAdapter(host="127.0.0.1", port=11111, trade_env=1)


def _mk_order(symbol="US.AAPL", qty=10, price=100.0, is_exit=False):
    meta = {"is_exit": True} if is_exit else {}
    return Order(symbol=symbol, direction=Direction.LONG,
                 order_type=OrderType.LIMIT, quantity=qty, price=price,
                 metadata=meta)


def _fake_capital(equity):
    """伪造 capital_safety 单例 (只暴露 _state.current_equity)."""
    return SimpleNamespace(_state=SimpleNamespace(current_equity=equity))


# ----------------------------------------------------------------------
# 1. 名义兜底门禁 (最后一道墙)
# ----------------------------------------------------------------------

class TestNotionalSanityCheck:
    def test_fail_close_when_equity_unavailable(self, adapter, monkeypatch):
        """equity 不可用必须拒绝 — 不允许以虚假 $100K 放行超大订单."""
        import app.risk.capital_safety as cs
        monkeypatch.setattr(cs, "get_capital_safety_system",
                            lambda: SimpleNamespace(_state=None))
        # _state=None → 属性访问抛异常 → except 吞掉 → equity=0 → fail-close
        assert adapter._notional_sanity_check("US.AAPL", 10, 100.0) is False

    def test_fail_close_when_getter_raises(self, adapter, monkeypatch):
        import app.risk.capital_safety as cs

        def _boom():
            raise RuntimeError("capital_safety 未就绪")
        monkeypatch.setattr(cs, "get_capital_safety_system", _boom)
        assert adapter._notional_sanity_check("US.AAPL", 10, 100.0) is False

    def test_rejects_over_30pct_equity(self, adapter, monkeypatch):
        import app.risk.capital_safety as cs
        monkeypatch.setattr(cs, "get_capital_safety_system",
                            lambda: _fake_capital(100_000.0))
        # 30% × $100K = $30K 上限; $35K 必须拒
        assert adapter._notional_sanity_check("US.AAPL", 100, 350.0) is False

    def test_passes_under_30pct_equity(self, adapter, monkeypatch):
        import app.risk.capital_safety as cs
        monkeypatch.setattr(cs, "get_capital_safety_system",
                            lambda: _fake_capital(100_000.0))
        # $25K < $30K 上限 → 放行 (放行方向也要验证, 防门禁只测拦截)
        assert adapter._notional_sanity_check("US.AAPL", 100, 250.0) is True

    def test_close_order_bypasses_cap(self, adapter, monkeypatch):
        """平仓单一律放行 (P0 6/16): 紧急退出不能被门禁阻断."""
        import app.risk.capital_safety as cs
        monkeypatch.setattr(cs, "get_capital_safety_system",
                            lambda: _fake_capital(100_000.0))
        # $430K 远超帽, 但 is_close=True → 放行
        assert adapter._notional_sanity_check(
            "US.SNDK", 1000, 430.0, is_close=True) is True

    def test_invalid_params_not_intercepted_here(self, adapter):
        """qty/price 非法不在此拦截 (上游职责), 返回 True."""
        assert adapter._notional_sanity_check("US.AAPL", 0, 100.0) is True
        assert adapter._notional_sanity_check("US.AAPL", 10, 0.0) is True


# ----------------------------------------------------------------------
# 2. submit_order 断连安全
# ----------------------------------------------------------------------

class TestSubmitOrderDisconnected:
    def test_sim_account_falls_back_to_simulated_fill(self, adapter, monkeypatch):
        """sim 断连 → 模拟成交 (保持链路可跑), 不抛异常."""
        monkeypatch.setenv("AUTO_TRADE_ACCOUNT", "sim")
        adapter.connected = False
        result = adapter.submit_order(_mk_order())
        assert result["status"] == "simulated_fill"
        assert result["order_id"]
        assert str(result["futu_order_id"]).startswith("SIM_")

    def test_real_account_refuses_when_disconnected(self, adapter, monkeypatch):
        """real 断连 → 拒绝提交 — 绝不在断连状态下实盘交易."""
        monkeypatch.setenv("AUTO_TRADE_ACCOUNT", "real")
        adapter.connected = False
        with pytest.raises(RuntimeError, match="实盘安全"):
            adapter.submit_order(_mk_order())


# ----------------------------------------------------------------------
# 3. _simulate_fill 结构契约
# ----------------------------------------------------------------------

class TestSimulateFillContract:
    def test_returns_required_fields(self, adapter):
        order = _mk_order()
        result = adapter._simulate_fill(order)
        assert result["order_id"] == order.order_id
        assert result["status"] == "simulated_fill"
        assert result["futu_order_id"] == f"SIM_{order.order_id[:8]}"


# ----------------------------------------------------------------------
# 原有测试 (保留)
# ----------------------------------------------------------------------

def test_futu_adapter_initialization():
    """测试富途适配器初始化"""
    adapter = FutuBrokerAdapter(
        host="127.0.0.1",
        port=11111,
        trade_env=1,  # 模拟环境
    )

    assert adapter.is_connected() == False  # 初始未连接

    # 注意：实际连接测试需要 FutuOpenD 运行
    # 这里只测试接口
