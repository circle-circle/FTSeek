# -*- coding: utf-8 -*-
"""手动单链路 P0-2/P0-3 契约测试 (2026-08-16, DEB-fullchain).

P0-2: 市价单 SL/TP 不再静默丢弃 — 成交价回填 + 未成交转 monitor 队列重试。
P0-3: 幂等 key 不再预烧 — 仅成功提交后记录; 持久层 SQLite + 指纹 key 兜底。
"""
import os
import sys
import time
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import pytest
from unittest.mock import patch, MagicMock

from app.web_dashboard.order import executor as exe


class FakeOrder:
    def __init__(self, symbol="US.TEST", action="BUY", quantity=10,
                 order_type="MARKET", price=None, order_id="ord_1"):
        self.symbol = symbol
        self.direction = "LONG" if action == "BUY" else "SHORT"
        self.action = action
        self.quantity = quantity
        self.order_type = order_type
        self.price = price
        self.reason = "test"
        self.mode = "simulate"
        self.time_in_force = "DAY"
        self.fill_outside_rth = False
        self.stop_loss = 95.0
        self.take_profit = 110.0
        self.acknowledge_price_warning = False
        self.timestamp = ""
        self.order_id = order_id
        self.status = "pending"
        self.error_msg = ""


class FakeService:
    def __init__(self):
        self._order_lock = threading.Lock()
        self._recent_keys = {}
        self._idempotency_ttl = 60
        self._broker_ctx = None
        self._connected = False

    def _is_live(self):
        return False


@pytest.fixture
def svc():
    return FakeService()


@pytest.fixture(autouse=True)
def force_memory_idempotency():
    """测试强制走内存 fallback (不碰真实 SQLite)."""
    with patch.object(exe, "_get_persistent_idempotency", return_value=None):
        yield


class TestP03IdempotencyNoPreburn:
    """P0-3 契约: key 只在成功提交后记录, check 不预烧."""

    def test_check_does_not_preburn(self, svc):
        """旧 bug 复现点: check 第一次不得写入 key (预烧)."""
        assert exe.check_idempotency(svc, "k1") is False
        assert "k1" not in svc._recent_keys, "check 预烧 key — P0-3 回归!"

    def test_failed_order_key_reusable(self, svc):
        """下单失败后同 key 可重试 (key 未记录)."""
        assert exe.check_idempotency(svc, "k1") is False
        # 模拟下单失败: 不调用 record_idempotency
        assert exe.check_idempotency(svc, "k1") is False, "失败后同 key 应可重试"

    def test_successful_order_blocks_retry(self, svc):
        """成功提交后同 key 被拒 (1h 窗口内去重)."""
        assert exe.check_idempotency(svc, "k1") is False
        exe.record_idempotency(svc, "k1", "ord_1")
        assert exe.check_idempotency(svc, "k1") is True, "成功后同 key 必须拒绝"

    def test_none_key_not_checked(self, svc):
        """key=None 不去重 (由 synthesize_idem_key 在 submit_order 兜底)."""
        assert exe.check_idempotency(svc, None) is False

    def test_synthesize_key_stable_within_bucket(self):
        """指纹 key: 30s 桶内同参数同 key (防双击), 跨桶不同 (不误拦)."""
        o = FakeOrder()
        k1 = exe.synthesize_idem_key(o)
        k2 = exe.synthesize_idem_key(o)
        # 同桶内稳定 (除非恰好跨桶边界, 用固定时间消除抖动)
        with patch.object(exe.time, "time", return_value=1000000.0):
            assert exe.synthesize_idem_key(o) == exe.synthesize_idem_key(o)
        with patch.object(exe.time, "time", return_value=1000000.0):
            k_a = exe.synthesize_idem_key(o)
        with patch.object(exe.time, "time", return_value=1000060.0):
            k_b = exe.synthesize_idem_key(o)
        assert k_a != k_b, "跨 30s 桶应落不同 key (不误拦合法重复)"
        assert "US.TEST" in k1 and "BUY" in k1


class TestP02MarketOrderProtection:
    """P0-2 契约: 市价单 SL/TP 不再静默丢弃."""

    def _mock_broker(self, svc, dealt_avg=100.5, dealt_qty=10):
        """mock broker order_list_query 返回成交数据."""
        svc._broker_ctx = MagicMock()
        svc._connected = True
        import pandas as pd
        data = pd.DataFrame([{"dealt_avg_price": dealt_avg, "dealt_qty": dealt_qty}])
        svc._broker_ctx.order_list_query.return_value = (0, data)  # RET_OK=0

    def test_backfill_fills_price(self, svc):
        """成交价回填: dealt_avg_price → order.price, dealt_qty → order.quantity."""
        order = FakeOrder(order_type="MARKET", price=None)
        self._mock_broker(svc, dealt_avg=100.5, dealt_qty=8)
        with patch("futu.TrdEnv") as _te:
            _te.REAL, _te.SIMULATE = 0, 1
            ok = exe._backfill_fill_price(svc, order)
        assert ok is True
        assert order.price == 100.5
        assert order.quantity == 8

    def test_backfill_no_fill_returns_false(self, svc):
        """未成交 (dealt_avg=0) → False, 不伪造价格."""
        order = FakeOrder(order_type="MARKET", price=None)
        self._mock_broker(svc, dealt_avg=0, dealt_qty=0)
        ok = exe._backfill_fill_price(svc, order)
        assert ok is False
        assert order.price is None, "未成交不得伪造成交价"

    def test_market_order_filled_places_protection(self, svc):
        """市价单已成交 → 直接挂保护单 (不静默丢弃)."""
        order = FakeOrder(order_type="MARKET", price=None)
        self._mock_broker(svc)
        with patch.object(exe, "_backfill_fill_price", return_value=True), \
             patch.object(exe, "place_protective_stop") as mock_place:
            exe._try_place_protection(svc, order)
        mock_place.assert_called_once_with(svc, order)

    def test_market_order_unfilled_enqueues_retry(self, svc):
        """市价单未成交 → 转 monitor 队列重试 (保护单不丢)."""
        order = FakeOrder(order_type="MARKET", price=None, order_id="ord_mx")
        exe._pending_protections.clear()
        with patch.object(exe, "_backfill_fill_price", return_value=False), \
             patch.object(exe, "_ensure_monitor_started"), \
             patch.object(exe, "place_protective_stop") as mock_place:
            exe._try_place_protection(svc, order)
        mock_place.assert_not_called()
        assert "ord_mx" in exe._pending_protections, "未成交市价单必须转入重试队列"
        exe._pending_protections.clear()

    def test_no_order_id_logs_error_not_silent(self, svc, caplog):
        """无 order_id → 显性 error 日志 (原静默丢失)."""
        order = FakeOrder(order_type="MARKET", price=None, order_id="")
        with patch.object(exe, "_backfill_fill_price", return_value=False), \
             caplog.at_level("ERROR", logger="ftseek_us.manual_order_service.executor"):
            exe._try_place_protection(svc, order)
        assert any("无 order_id" in r.message for r in caplog.records), \
            "无 order_id 必须显性告警, 禁止静默"
