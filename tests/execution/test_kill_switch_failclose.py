"""P1.2 (RFC-060): Kill Switch fail-close 单元测试

验证 check_kill_switch() 抛异常时, 入场订单被 REJECTED (fail-close),
而非静默放行 (原 fail-open 会绕过紧急制动).

手动线被测点: app/execution/engine.py ExecutionEngine.submit_order (line ~227-248)
自动线同源逻辑: app/pipeline/order_executor.py (line ~3547-3555)
"""
import os
import sys

# 项目根加入 path (独立可跑)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ.setdefault("TRADE_ENV", "1")

from unittest.mock import patch

import pytest

from app.execution.engine import ExecutionEngine
from app.core.models import Order, OrderState, Direction, OrderType


class _StubEventStore:
    """最小 event_store stub — kill-switch 早返回, 这些方法不会被真正调用."""
    def append(self, event): pass
    def record_alert(self, alert): pass
    def record_failure(self, failure): pass


def _entry_order(symbol="US.AAPL", client_order_id="FTUS_test_killswitch_1"):
    """MAIN 入场单: client_order_id 非 exit_/orb_/scalping_, 非 ORB 标的, 无 is_exit."""
    return Order(
        client_order_id=client_order_id,
        symbol=symbol,
        direction=Direction.LONG,
        order_type=OrderType.LIMIT,
        quantity=10,
        price=180.0,
        metadata={},
    )


def test_kill_switch_check_exception_rejects_entry_fail_close():
    """P1.2 核心: check_kill_switch 抛异常 → 入场单必须 REJECTED (fail-close)."""
    engine = ExecutionEngine(config={}, event_store=_StubEventStore())
    order = _entry_order(client_order_id="FTUS_test_ks_failclose_unique")

    with patch(
        "app.us_pipeline.emergency_interceptor.check_kill_switch",
        side_effect=RuntimeError("simulated lookup failure"),
    ):
        result = engine.submit_order(order)

    assert result.state == OrderState.REJECTED, \
        f"期望 REJECTED (fail-close), 实际 {result.state}"
    assert "fail-close" in (result.failure_reason or ""), \
        f"failure_reason 应含 'fail-close': {result.failure_reason}"


def test_kill_switch_active_rejects_entry():
    """对照: kill switch 正常激活 → 也 REJECTED (原有 fail-close 逻辑保留)."""
    engine = ExecutionEngine(config={}, event_store=_StubEventStore())
    order = _entry_order(client_order_id="FTUS_test_ks_active_unique")

    with patch(
        "app.us_pipeline.emergency_interceptor.check_kill_switch",
        return_value=True,
    ):
        result = engine.submit_order(order)

    assert result.state == OrderState.REJECTED
    assert "Kill Switch" in (result.failure_reason or "")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
