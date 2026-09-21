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
