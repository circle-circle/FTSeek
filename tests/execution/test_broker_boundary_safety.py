import time
from types import SimpleNamespace

import pandas as pd
from futu import TrdSide
import pytest

import app.pipeline.signal_pipeline as signal_pipeline
import app.utils.daily_active_pool as daily_active_pool

from app.coordinator.capital_allocator import CapitalAllocator
from app.core.models import Direction, Order, OrderState, OrderType, Position
from app.core.scheduler_core import PositionStore
from app.execution.broker_adapter import FutuBrokerAdapter
from app.pipeline.order_executor import (
    _close_pre_check,
    _submit_order_optimized,
    _release_reserved_capital,
    _reserve_capital_for_order,
)
from app.pipeline.signal_pipeline import _check_execution_rules


class _DisconnectedAdapter:
    connected = False

    def is_connected(self):
        return False

    def get_positions(self):
        raise RuntimeError("broker unavailable")


class _Signal:
    strategy_id = "playbook"
    metadata = {"strategy_id": "playbook"}


def test_close_pre_check_skips_when_position_is_unknown():
    scheduler = SimpleNamespace(
        execution_engine=SimpleNamespace(broker_adapter=_DisconnectedAdapter()),
        position_store=SimpleNamespace(has=lambda symbol: True),
    )
    position = Position(
        symbol="US.AAPL",
        direction=Direction.LONG,
        size=10,
        avg_entry_price=100.0,
    )

    reason = _close_pre_check(scheduler, position)

    assert reason == "broker 持仓状态未知，跳过本轮平仓"


def test_order_capital_reservation_uses_injected_allocator(monkeypatch):
    import app.risk.capital_safety as capital_safety

    allocator = CapitalAllocator(
        caps={"playbook": 0.65},
        observe_only=False,
    )
    scheduler = SimpleNamespace(
        coordinator=SimpleNamespace(capital=allocator),
    )
    monkeypatch.setattr(
        capital_safety,
        "get_capital_safety_system",
        lambda: SimpleNamespace(_state=SimpleNamespace(current_equity=100_000.0)),
    )
    context = {}

    reserved = _reserve_capital_for_order(
        scheduler,
        _Signal(),
        {"strategy_id": "playbook"},
        "US.AAPL",
        10,
        100.0,
        context,
    )

    assert reserved is True
    assert context["_capital_reserved_strategy"] == "playbook"
    assert context["_capital_reserved_pct"] == 0.01
    assert allocator.snapshot().by_strategy["playbook"] == 0.01

    _release_reserved_capital(scheduler, context)
    assert allocator.snapshot().by_strategy["playbook"] == 0.0


def test_order_capital_reservation_rejects_unknown_bucket(monkeypatch):
    import app.risk.capital_safety as capital_safety

    allocator = CapitalAllocator(
        caps={"playbook": 0.65},
        observe_only=False,
    )
    scheduler = SimpleNamespace(
        coordinator=SimpleNamespace(capital=allocator),
    )
    monkeypatch.setattr(
        capital_safety,
        "get_capital_safety_system",
        lambda: SimpleNamespace(_state=SimpleNamespace(current_equity=100_000.0)),
    )
    context = {}

    reserved = _reserve_capital_for_order(
        scheduler,
        _Signal(),
        {"strategy_id": "unknown"},
        "US.AAPL",
        10,
        100.0,
        context,
    )

    assert reserved is False
    assert "_capital_reserved_strategy" not in context
    assert all(value == 0.0 for value in allocator.snapshot().by_strategy.values())


def test_repeated_capital_reservation_is_idempotent(monkeypatch):
    import app.risk.capital_safety as capital_safety

    allocator = CapitalAllocator(
        caps={"playbook": 0.65},
        observe_only=False,
    )
    scheduler = SimpleNamespace(
        coordinator=SimpleNamespace(capital=allocator),
    )
    monkeypatch.setattr(
        capital_safety,
        "get_capital_safety_system",
        lambda: SimpleNamespace(_state=SimpleNamespace(current_equity=100_000.0)),
    )
    context = {}

    assert _reserve_capital_for_order(
        scheduler, _Signal(), {"strategy_id": "playbook"}, "US.AAPL", 10, 100.0, context
    )
    assert _reserve_capital_for_order(
        scheduler, _Signal(), {"strategy_id": "playbook"}, "US.AAPL", 10, 100.0, context
    )

    assert allocator.snapshot().by_strategy["playbook"] == 0.01


def test_execution_rules_fail_closed_for_missing_or_invalid_inputs():
    valid_context = {
        "prev_close": 100.0,
        "open_price": 101.0,
        "current_price": 101.5,
    }
    rules = {"gap_up_over_2pct": "SKIP"}

    assert _check_execution_rules(rules, "US.AAPL", valid_context) is True
    assert _check_execution_rules(None, "US.AAPL", valid_context) is False
    assert _check_execution_rules([], "US.AAPL", valid_context) is False
    assert _check_execution_rules(rules, "US.AAPL", {"prev_close": 0, "open_price": 101, "current_price": 101}) is False
    assert _check_execution_rules(rules, "US.AAPL", {"prev_close": 100, "open_price": "bad", "current_price": 101}) is False


def test_submit_order_optimized_rejects_zero_notional_without_submitting():
    class ExecutionEngine:
        def __init__(self):
            self.submitted = []

        def submit_order(self, order):
            self.submitted.append(order)
            return order

    engine = ExecutionEngine()
    scheduler = SimpleNamespace(execution_engine=engine)
    order = Order(
        symbol="US.AAPL",
        direction=Direction.LONG,
        order_type=OrderType.MARKET,
        quantity=10,
        price=0.0,
        trd_side="BUY",
    )

    submitted, metadata = _submit_order_optimized(
        scheduler, order, signal_price=0.0, signal_time=time.time(),
        direction=Direction.LONG,
    )

    assert submitted.state == OrderState.REJECTED
    assert submitted.last_error == "invalid_order_price_or_quantity"
    assert metadata["error"] == "invalid_order_price_or_quantity"
    assert engine.submitted == []


class _FakeTradeContext:
    def __init__(self):
        self.captured = None

    def place_order(self, **kwargs):
        self.captured = kwargs
        return 0, pd.DataFrame({"order_id": ["FUTU_123"]})

    def order_list_query(self, **kwargs):
        return 0, pd.DataFrame([{
            "order_status": "FILLED_ALL",
            "dealt_qty": 10,
            "dealt_avg_price": 100.0,
        }])


def _connected_adapter():
    adapter = FutuBrokerAdapter(host="127.0.0.1", port=11111, trade_env=1)
    adapter.connected = True
    adapter._last_heartbeat_time = time.time()
    adapter.trade_ctx = object()
    return adapter


def test_broker_submit_rejects_missing_or_invalid_trd_side(monkeypatch):
    adapter = _connected_adapter()
    monkeypatch.setattr(adapter, "_notional_sanity_check", lambda *args, **kwargs: True)

    missing_side = Order(symbol="US.AAPL", direction=Direction.SHORT, quantity=10, price=100.0)
    invalid_side = Order(
        symbol="US.AAPL", direction=Direction.SHORT, quantity=10, price=100.0,
        trd_side="SHORT",
    )

    assert adapter.submit_order(missing_side)["error"] == "missing_trd_side"
    assert adapter.submit_order(invalid_side)["error"] == "invalid_trd_side"


def test_broker_submit_uses_explicit_short_open_side(monkeypatch):
    adapter = _connected_adapter()
    trade_context = _FakeTradeContext()
    adapter.trade_ctx = trade_context
    monkeypatch.setattr(adapter, "_notional_sanity_check", lambda *args, **kwargs: True)
    order = Order(
        symbol="US.AAPL",
        direction=Direction.SHORT,
        order_type=OrderType.LIMIT,
        quantity=10,
        price=100.0,
        trd_side="SELL_SHORT",
    )

    result = adapter.submit_order(order)

    assert result["status"] == "submitted"
    assert trade_context.captured["trd_side"] == TrdSide.SELL_SHORT


def test_position_store_rejects_opposite_direction_overwrite(monkeypatch):
    monkeypatch.setattr(
        daily_active_pool,
        "get_daily_active_pool",
        lambda *args, **kwargs: SimpleNamespace(
            add_position=lambda *args, **kwargs: None
        ),
    )
    store = PositionStore()
    long_position = Position(
        symbol="US.AAPL", direction=Direction.LONG, size=10, avg_entry_price=100.0
    )
    short_position = Position(
        symbol="US.AAPL", direction=Direction.SHORT, size=10, avg_entry_price=100.0
    )

    assert store.add("US.AAPL", long_position) is True
    assert store.add("US.AAPL", short_position) is False
    assert store.get("US.AAPL").direction == Direction.LONG


def test_market_history_updates_are_thread_safe_and_bounded(monkeypatch):
    monkeypatch.setattr(signal_pipeline, "_VIX_HISTORY", [])
    monkeypatch.setattr(signal_pipeline, "_SPY_HISTORY", [])
    now = time.time()

    for index in range(100):
        signal_pipeline._record_market_history(
            now + index, 20.0 + index, 500.0 + index
        )

    vix_history, spy_history = signal_pipeline._get_market_history()

    assert len(vix_history) <= signal_pipeline._HISTORY_MAX_ENTRIES
    assert len(spy_history) <= signal_pipeline._HISTORY_MAX_ENTRIES
    assert vix_history[-1] == pytest.approx(119.0)
    assert spy_history[-1] == pytest.approx(599.0)
