# tests/execution/test_broker_adapter.py
"""FutuBrokerAdapter 契约测试 (1194 行真实下单通道).

B2 (2026-08-24, OKR-2026-09 基建, 体检 M1 收窄版): 原仅 16 行初始化测试.
补齐三条资金路径契约 (全部离线, 不触真实连接):
  1. 名义兜底门禁 _notional_sanity_check: fail-close / 30% 硬帽 / 平仓豁免
  2. submit_order 断连安全: sim→模拟成交, real→拒绝 (不允许断连交易)
  3. _simulate_fill 返回结构契约
"""
import os
import time
from types import SimpleNamespace

import pandas as pd
import pytest
from futu import TrdEnv

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


class _FakePositionCtx:
    def __init__(self, rows):
        self._rows = rows

    def position_list_query(self, **kwargs):
        return 0, pd.DataFrame(self._rows)


class _FakeAccountCtx:
    def __init__(self, rows):
        self._rows = rows

    def get_acc_list(self):
        return 0, pd.DataFrame(self._rows)


def _connected_adapter(trade_env=1, account_id=""):
    adapter = FutuBrokerAdapter(
        host="127.0.0.1", port=11111,
        trade_env=trade_env, account_id=account_id,
    )
    adapter.connected = True
    adapter._last_heartbeat_time = time.time()
    return adapter


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
        """qty 非法不在此拦截 (上游职责), 返回 True."""
        assert adapter._notional_sanity_check("US.AAPL", 0, 100.0) is True

    def test_market_order_fail_close_without_price(self, adapter):
        """DEB-20260904 #182: 市价单 (price<=0) 估不到价时 fail-close 拒绝,
        不再像旧逻辑那样直接放行绕过 30% 兜底."""
        # 测试进程无注册的 _shared_router → 估价为 0 → 拒绝
        assert adapter._notional_sanity_check("US.AAPL", 10, 0.0) is False


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

    def test_real_account_refuses_when_disconnected(self, monkeypatch):
        """real 断连 → 拒绝提交 — 绝不在断连状态下实盘交易.

        DEB-20260903 #97: 实盘判定按 self.trade_env 而非 AUTO_TRADE_ACCOUNT 环境变量,
        故此处必须构造 trade_env=0 的实盘适配器 (共享 fixture 是 trade_env=1 模拟)."""
        real_adapter = FutuBrokerAdapter(host="127.0.0.1", port=11111, trade_env=0)
        real_adapter.connected = False
        with pytest.raises(RuntimeError, match="实盘安全"):
            real_adapter.submit_order(_mk_order())


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


class TestCrossMarketPositionIsolation:
    def test_get_positions_filters_non_us_rows(self):
        adapter = _connected_adapter()
        adapter.trade_ctx = _FakePositionCtx([
            {"code": "HK.00700", "qty": 300, "cost_price": 439.6,
             "market_val": 125700, "pl_val": -6180, "pl_ratio": -4.69},
            {"code": "US.AAPL", "qty": 10, "cost_price": 100,
             "market_val": 1000, "pl_val": 100, "pl_ratio": 10},
        ])

        positions = adapter.get_positions()

        assert [p["symbol"] for p in positions] == ["US.AAPL"]

    def test_get_option_positions_filters_non_us_rows(self):
        adapter = _connected_adapter()
        adapter.trade_ctx = _FakePositionCtx([
            {"code": "HK.00700", "qty": 1, "cost_price": 1,
             "market_val": 1, "pl_val": 0, "pl_ratio": 0,
             "security_type": "OPTION"},
            {"code": "US.AAPL260918C00100000", "qty": 1, "cost_price": 1,
             "market_val": 100, "pl_val": 0, "pl_ratio": 0,
             "security_type": "OPTION"},
        ])

        positions = adapter.get_option_positions()

        assert [p["option_code"] for p in positions] == [
            "US.AAPL260918C00100000"
        ]


class TestAccountDiscoveryAmbiguity:
    def test_real_multiple_accounts_without_explicit_id_fails(self, monkeypatch):
        monkeypatch.delenv("REAL_ACCOUNT_ID", raising=False)
        adapter = _connected_adapter(trade_env=0)
        adapter.trade_ctx = _FakeAccountCtx([
            {"acc_id": 111, "trd_env": TrdEnv.REAL},
            {"acc_id": 222, "trd_env": TrdEnv.REAL},
        ])

        assert adapter._discover_real_account() is False
        assert adapter.account_id == ""

    def test_real_explicit_id_is_validated(self, monkeypatch):
        monkeypatch.setenv("REAL_ACCOUNT_ID", "222")
        adapter = _connected_adapter(trade_env=0)
        adapter.trade_ctx = _FakeAccountCtx([
            {"acc_id": 111, "trd_env": TrdEnv.REAL},
            {"acc_id": 222, "trd_env": TrdEnv.REAL},
        ])

        assert adapter._discover_real_account() is True
        assert adapter.account_id == "222"

    def test_simulate_multiple_accounts_without_explicit_id_fails(self, monkeypatch):
        monkeypatch.delenv("SIMULATE_ACCOUNT_ID", raising=False)
        adapter = _connected_adapter(account_id="")
        adapter.trade_ctx = _FakeAccountCtx([
            {"acc_id": 111, "trd_env": TrdEnv.SIMULATE},
            {"acc_id": 222, "trd_env": TrdEnv.SIMULATE},
        ])

        assert adapter._discover_simulate_account() is False
        assert adapter.account_id == ""


# ----------------------------------------------------------------------
# 4. REAL HKD 会计转换 overlay 契约 (2026-09-21 全链路复查 P0-1 回归防护)
# ----------------------------------------------------------------------

class _FakeAccinfoCtx:
    def __init__(self, row):
        self._row = row

    def accinfo_query(self, **kwargs):
        return 0, pd.DataFrame([self._row])


class TestRealHkdAccountingOverlay:
    """get_account_info() REAL HKD 转换后必须完整 overlay 共享转换器输出.

    P0-1 本体: 旧实现 `if key in result or key.startswith("_")` 会把
    long_mv/short_mv/fx/raw_currency/maintenance_margin/max_power_short/
    margin_call_margin/interest_charged_amount 静默丢弃。
    合成行: total=100000 HKD, hkd_assets=22000, usd_assets=10000
    → fx = 10000/(100000-22000) = 0.1282051…
    """

    _HKD_ROW = {
        "currency": "HKD",
        "total_assets": 100000.0,
        "hkd_assets": 22000.0,
        "usd_assets": 10000.0,
        "cash": 7800.0,
        "us_cash": 5000.0,          # 与折算值 1000.0 不一致 → 弃用
        "market_val": 50000.0,
        "long_mv": 60000.0,
        "short_mv": -10000.0,
        "power": 20000.0,
        "usd_net_cash_power": 0.0,
        "avl_withdrawal_cash": 15000.0,
        "us_avl_withdrawal_cash": 0.0,
        "max_power_short": 8000.0,
        "margin_call_margin": 0.0,
        "interest_charged_amount": 78.0,
        "maintain_margin": 5000.0,
        "debt": 2000.0,
    }

    def _info(self, row=None):
        adapter = _connected_adapter(trade_env=0, account_id="28170000")
        adapter.trade_ctx = _FakeAccinfoCtx(row or self._HKD_ROW)
        return adapter.get_account_info()

    def test_overlay_exposes_full_converted_contract(self):
        result = self._info()
        assert result["currency"] == "USD"
        assert result["raw_currency"] == "HKD"
        assert result["account_scope"] == "global"
        assert abs(result["fx"] - 10000.0 / 78000.0) < 1e-9
        assert result["total_assets"] == 12820.51
        assert result["cash"] == 1000.0
        assert "弃用" in result["_cash_source"]
        # P0-1 本体: 曾被条件 overlay 静默丢弃的字段
        assert result["long_mv"] == 7692.31
        assert result["short_mv"] == 1282.05
        assert result["max_power_short"] == 1025.64
        assert result["interest_charged_amount"] == 10.0
        assert result["maintenance_margin"] == 641.03
        assert result["market_val"] == 6410.26
        assert result["debt"] == 256.41
        assert result["buying_power"] == 2564.1

    def test_unknown_currency_without_rate_fails_close(self):
        # SGD 无派生路径 (sgd_assets 缺失) 且无固定汇率兜底 → ValueError → {}
        result = self._info(dict(self._HKD_ROW, currency="SGD"))
        assert result == {}


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
