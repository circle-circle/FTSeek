# -*- coding: utf-8 -*-
"""ExecutionEngine AutoGate 读异常 fail-close 契约 (2026-10-08 运维体检批).

病根: engine.submit_order 的 auto_trading_enabled 裸 regex 直读 (AGENT.md 裁定的
刻意设计, 绕过热重载 60s 延迟) 原实现 `except Exception: pass` — YAML 读不到时
自动引擎入场被放行。注释声称 "order_executor 有自己的独立检查", 但那条检查读
ModeSwitchService 内存态, 与本处文件直读是两个来源, 兜不住同一次判定。

钉两件事:
  1. YAML 读取异常 → ORB/MAIN 自动入场单 REJECTED (fail-close), 手动单不受影响;
  2. YAML 可读且 auto_trading_enabled=false → 照旧 REJECTED (原有语义不回退)。
"""
import builtins
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ.setdefault("TRADE_ENV", "1")

import pytest

from app.execution.engine import ExecutionEngine
from app.core.models import Order, OrderState, Direction, OrderType


class _StubEventStore:
    def append(self, event): pass
    def record_alert(self, alert): pass
    def record_failure(self, failure): pass


def _entry_order(symbol="US.AAPL", client_order_id="FTUS_test_autogate_1"):
    return Order(
        client_order_id=client_order_id,
        symbol=symbol,
        direction=Direction.LONG,
        order_type=OrderType.LIMIT,
        quantity=10,
        price=180.0,
        metadata={},
    )


def _patch_kill_switch_off(monkeypatch):
    monkeypatch.setattr(
        "app.us_pipeline.emergency_interceptor.check_kill_switch", lambda: False)


def test_auto_gate_yaml_read_error_rejects_entry_fail_close(monkeypatch):
    """核心钉: 配置读不到 → 自动入场 fail-close, 不得静默放行。"""
    engine = ExecutionEngine(config={}, event_store=_StubEventStore())
    order = _entry_order(client_order_id="FTUS_test_autogate_failclose")
    _patch_kill_switch_off(monkeypatch)

    real_open = builtins.open

    def fail_config_open(file, *args, **kwargs):
        if str(file).endswith("config/unified_config.yaml"):
            raise OSError("simulated config read failure")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fail_config_open)
    result = engine.submit_order(order)

    assert result.state == OrderState.REJECTED, (
        f"期望 REJECTED (fail-close), 实际 {result.state}")
    assert "auto_trading_enabled" in (result.failure_reason or "")
    assert "fail-close" in (result.failure_reason or "")


def test_auto_gate_disabled_value_still_rejects(monkeypatch):
    """对照: 文件可读且 false → 原有阻断语义不变。"""
    engine = ExecutionEngine(config={}, event_store=_StubEventStore())
    order = _entry_order(client_order_id="FTUS_test_autogate_disabled")
    _patch_kill_switch_off(monkeypatch)

    real_open = builtins.open

    def fake_config_open(file, *args, **kwargs):
        if str(file).endswith("config/unified_config.yaml"):
            return io.StringIO("feature_flags:\n  auto_trading_enabled: false\n")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fake_config_open)
    result = engine.submit_order(order)

    assert result.state == OrderState.REJECTED
    assert "auto_trading_enabled=false" in (result.failure_reason or "")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
