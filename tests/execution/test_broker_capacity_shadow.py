"""P0-2 stage-1 broker capacity shadow contracts (record-only)."""
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from futu import TrdEnv

from app.execution.broker_capacity_shadow import (
    build_capacity_record,
    capacity_field_for_order,
    query_active_orders,
    query_capacity,
    record_order_capacity_shadow,
)


def _order(direction="LONG", *, is_exit=False, symbol="US.MU", qty=9, price=120.5):
    return SimpleNamespace(
        symbol=symbol,
        direction=direction,
        quantity=qty,
        price=price,
        client_order_id="pb_test_1_real",
        metadata={"is_exit": is_exit, "reference_price": price},
    )


@pytest.mark.parametrize(
    ("direction", "is_exit", "expected"),
    [
        ("LONG", False, "max_cash_buy"),
        ("LONG", True, "max_position_sell"),
        ("SHORT", False, "max_sell_short"),
        ("SHORT", True, "max_buy_back"),
    ],
)
def test_capacity_field_mapping_is_four_way(direction, is_exit, expected):
    assert capacity_field_for_order(direction, is_exit) == expected


class _CapacityCtx:
    def __init__(self, row=None, *, capacity_error=None, order_rows=None, order_error=None):
        self.row = row
        self.capacity_error = capacity_error
        self.order_rows = order_rows or []
        self.order_error = order_error
        self.capacity_calls = []
        self.order_calls = []

    def acctradinginfo_query(self, **kwargs):
        self.capacity_calls.append(kwargs)
        if self.capacity_error:
            raise RuntimeError(self.capacity_error)
        return 0, pd.DataFrame([self.row or {}])

    def order_list_query(self, **kwargs):
        self.order_calls.append(kwargs)
        if self.order_error:
            raise RuntimeError(self.order_error)
        return 0, pd.DataFrame(self.order_rows)


def _capacity_row(**overrides):
    row = {
        "max_cash_buy": 347,
        "max_cash_and_margin_buy": 347,
        "max_position_sell": 0,
        "max_sell_short": 49,
        "max_buy_back": 0,
        "long_required_im": 0,
        "short_required_im": 0,
        "session": "RTH",
    }
    row.update(overrides)
    return row


def test_capacity_records_sufficient_and_insufficient():
    ctx = _CapacityCtx(_capacity_row(), order_rows=[])
    sufficient = query_capacity(
        ctx, symbol="US.MU", price=100.0, trd_env=TrdEnv.REAL, acc_id="1",
        capacity_field="max_cash_buy", requested_qty=300,
    )
    insufficient = query_capacity(
        ctx, symbol="US.MU", price=100.0, trd_env=TrdEnv.REAL, acc_id="1",
        capacity_field="max_cash_buy", requested_qty=400,
    )

    assert sufficient["capacity_status"] == "sufficient"
    assert sufficient["capacity_qty"] == 347
    assert sufficient["capacity_all"]["session"] == "RTH"
    assert insufficient["capacity_status"] == "insufficient"


def test_capacity_query_exception_is_unknown_and_fail_open():
    ctx = _CapacityCtx(capacity_error="OpenD timeout")
    result = query_capacity(
        ctx, symbol="US.MU", price=100.0, trd_env=TrdEnv.REAL, acc_id=1,
        capacity_field="max_cash_buy", requested_qty=10,
    )

    assert result["capacity_status"] == "unknown"
    assert result["capacity_qty"] is None
    assert "OpenD timeout" in result["capacity_error"]


def test_active_orders_identify_external_manual_orders():
    ctx = _CapacityCtx(order_rows=[{
        "order_id": "OWN-1",
        "code": "US.MU",
        "trd_side": "SELL",
        "order_type": "STOP",
        "order_status": "SUBMITTED",
        "qty": 9.0,
        "dealt_qty": 0.0,
        "price": 0.0,
        "aux_price": 99.0,
        "remark": "pb_stop_US.MU_real",
    }, {
        "order_id": "MANUAL-1",
        "code": "US.MU",
        "trd_side": "BUY",
        "order_type": "NORMAL",
        "order_status": "SUBMITTED",
        "qty": 2.0,
        "dealt_qty": 0.0,
        "price": 100.0,
        "aux_price": 0.0,
        "remark": "manual",
    }, {
        "order_id": "DONE-1",
        "code": "US.MU",
        "trd_side": "SELL",
        "order_type": "NORMAL",
        "order_status": "FILLED_ALL",
        "qty": 1.0,
        "dealt_qty": 1.0,
        "price": 100.0,
        "aux_price": 0.0,
        "remark": "done",
    }])

    result = query_active_orders(
        ctx, symbol="US.MU", trd_env=TrdEnv.REAL, acc_id=1,
    )

    assert result["success"] is True
    assert result["active_order_count"] == 2
    assert result["external_active_order_count"] == 1
    assert {order["order_id"] for order in result["orders"]} == {"OWN-1", "MANUAL-1"}


def test_record_fail_open_when_jsonl_write_fails(tmp_path):
    ctx = _CapacityCtx(_capacity_row(), order_rows=[])
    bad_path = tmp_path / "not_a_file"
    bad_path.mkdir()

    record = record_order_capacity_shadow(
        ctx,
        acc_id="1",
        trd_env=TrdEnv.REAL,
        lock=None,
        order=_order(),
        target_env="real",
        is_exit=False,
        path=bad_path,
    )

    assert record is not None
    assert record["schema_version"] == "broker_capacity_shadow.v1"
    assert record["requested_qty"] == 9
    assert record["quantity_unit"] == "shares"
    assert record["capacity_status"] == "sufficient"
    assert record["capacity_field"] == "max_cash_buy"


def test_record_writes_one_jsonl_row_with_active_order_evidence(tmp_path):
    ctx = _CapacityCtx(_capacity_row(max_cash_buy=8), order_rows=[{
        "order_id": "MANUAL-1",
        "code": "US.MU",
        "trd_side": "BUY",
        "order_type": "NORMAL",
        "order_status": "SUBMITTED",
        "qty": 2.0,
        "dealt_qty": 0.0,
        "price": 100.0,
        "aux_price": 0.0,
        "remark": "manual",
    }])
    target = tmp_path / "capacity.jsonl"

    record = record_order_capacity_shadow(
        ctx, acc_id="1", trd_env=TrdEnv.REAL, lock=None,
        order=_order(qty=9), target_env="real", is_exit=False, path=target,
    )

    assert record["capacity_status"] == "insufficient"
    assert record["external_active_order_count"] == 1
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert '"record_only":true' in lines[0]


def test_build_record_uses_reference_price_for_market_order():
    order = _order(price=0.0)
    order.metadata = {"reference_price": 120.5}
    record = build_capacity_record(order, target_env="real", is_exit=False)
    assert record["price_usd"] == 120.5


def test_sim_order_never_queries_capacity():
    ctx = _CapacityCtx(_capacity_row())
    result = record_order_capacity_shadow(
        ctx, acc_id="1", trd_env=TrdEnv.SIMULATE, lock=None,
        order=_order(), target_env="sim", is_exit=False,
    )
    assert result is None
    assert ctx.capacity_calls == []
    assert ctx.order_calls == []
