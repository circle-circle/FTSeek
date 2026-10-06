# tests/execution/test_dual_broker_positions.py
"""DualBrokerAdapter.get_positions 跨市场隔离契约.

2026-09-21 全链路复查 P0-2 回归防护: REAL 综合账户 position_list_query 可能
返回港股等跨市场持仓, 必须与 broker_adapter.get_positions() 同口径按 "US."
前缀隔离; SIM (全 US.*) 行为零变化。全部离线, 不触真实连接。
"""
import threading

import pandas as pd

from app.execution.dual_broker import DualBrokerAdapter


class _FakeCtx:
    def __init__(self, rows):
        self._rows = rows

    def position_list_query(self, **kwargs):
        return 0, pd.DataFrame(self._rows)


def _adapter_with(rows):
    adapter = DualBrokerAdapter()
    ctx = _FakeCtx(rows)
    adapter._ctx_and_acc = lambda target_env: (ctx, "19930000", None, threading.Lock())
    return adapter


def test_filters_non_us_rows():
    adapter = _adapter_with([
        {"code": "HK.00700", "qty": 300, "market_val": 125700, "pl_val": -6180},
        {"code": "US.AAPL", "qty": 10, "market_val": 1000, "pl_val": 100},
    ])
    positions = adapter.get_positions(target_env="sim")
    assert [p["code"] for p in positions] == ["US.AAPL"]


def test_all_us_rows_preserved():
    adapter = _adapter_with([
        {"code": "US.AAPL", "qty": 10},
        {"code": "US.TSLA", "qty": 5},
    ])
    assert [p["code"] for p in adapter.get_positions()] == ["US.AAPL", "US.TSLA"]


def test_only_foreign_rows_returns_empty():
    adapter = _adapter_with([
        {"code": "HK.00700", "qty": 300},
        {"code": "SH.600519", "qty": 100},
    ])
    assert adapter.get_positions() == []


def test_empty_dataframe_returns_empty_list():
    assert _adapter_with([]).get_positions() == []


class _FakeOrderCtx:
    """捕获 place_order 实参的伪交易上下文 (STOP 单参数契约钉)."""

    def __init__(self):
        self.calls = []

    def place_order(self, **kwargs):
        self.calls.append(kwargs)
        return 0, pd.DataFrame([{"order_id": "TEST-STOP-1"}])  # RET_OK=0


def _adapter_with_order_ctx(ctx):
    adapter = DualBrokerAdapter()
    adapter._ctx_and_acc = lambda target_env: (ctx, "19930000", None, threading.Lock())
    return adapter


def test_live_stop_order_uses_aux_price_and_gtc():
    """2026-10-06 首笔 LIVE 探针事故钉: 富途 STOP 触发价必须走 aux_price
    (只传 price 被拒「下单缺少必要参数触发价格」), price=0=触发后市价,
    time_in_force 必须 GTC (DAY 当日收盘失效, W_WEEK 持仓跨日失去兜底)。
    生产先例: executor.py:1236 看板手动止损链 + probe_sim_stop_20260805.py:44。
    """
    ctx = _FakeOrderCtx()
    adapter = _adapter_with_order_ctx(ctx)
    result = adapter.place_protective_stop(
        "US.TEST", 1, "LONG", 22.96, target_env="real")

    assert result["success"] is True
    assert result["futu_order_id"] == "TEST-STOP-1"
    assert len(ctx.calls) == 1
    call = ctx.calls[0]
    assert call["aux_price"] == 22.96          # 触发价 (事故根因位)
    assert call["price"] == 0                  # 触发后市价成交
    assert call["trail_spread"] == 0           # 非追踪止损
    from futu import TimeInForce, OrderType as FutuOrderType, TrdSide
    assert call["time_in_force"] == TimeInForce.GTC
    assert call["order_type"] == FutuOrderType.STOP
    assert call["trd_side"] == TrdSide.SELL    # LONG 持仓 → SELL STOP


def test_live_stop_short_side_buys_to_cover():
    ctx = _FakeOrderCtx()
    adapter = _adapter_with_order_ctx(ctx)
    result = adapter.place_protective_stop(
        "US.TEST", 2, "SHORT", 30.5, target_env="real")

    assert result["success"] is True
    from futu import TrdSide
    assert ctx.calls[0]["trd_side"] == TrdSide.BUY  # SHORT 持仓 → BUY STOP


def test_sim_stop_still_skipped_without_order():
    """sim 臂不支持 STOP 的既有语义不变 (规则引擎兜底)."""
    ctx = _FakeOrderCtx()
    adapter = _adapter_with_order_ctx(ctx)
    result = adapter.place_protective_stop(
        "US.TEST", 1, "LONG", 22.96, target_env="sim")
    assert result.get("skipped") is True
    assert ctx.calls == []                      # sim 不触券商
