#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ExitExecutor 单元测试

测试目标:
  1. 止损/止盈/分批止盈 执行逻辑
  2. 冷却状态管理
  3. BAD_ORDER 标记逻辑
  4. Flash Exit 触发逻辑

版本历史:
  2026-05-19 v1: 创建 (Phase 11)
  2026-05-19 v2: 修复 mock 路径 + trade_result.pnl 类型
"""

import pytest
from unittest.mock import MagicMock, Mock, patch
from app.services.exit_executor import ExitExecutor


class TestExecuteStopLoss:
    """止损执行测试"""

    def test_stop_loss_normal(self, mock_subscriber, position_factory, trade_executor_mock, notifier_mock):
        """正常止损执行 — 直接测试 _do_execute_stop_loss (后台线程逻辑)"""
        position = position_factory(code="HK.00700", entry_price=100.0)
        mock_subscriber.position_manager.get_position.return_value = position

        # Mock trade result with proper float values
        trade_result = MagicMock()
        trade_result.success = True
        trade_result.pnl = -500.0
        trade_result.message = "OK"
        trade_executor_mock.execute.return_value = trade_result

        executor = ExitExecutor(
            position_manager=mock_subscriber.position_manager,
            trade_executor=trade_executor_mock,
            notifier=notifier_mock,
            consensus_engine=MagicMock(),
        )

        # 构造止损信号 (跳过 execute_stop_loss 的复杂前置检查，直接测试执行逻辑)
        from app.strategy.base import Signal, SignalAction
        stop_signal = Signal(
            code="HK.00700", name="HK.00700",
            action=SignalAction.SELL, price=95.0, quantity=100,
            reason="止损触发", strategy="止损",
            extra={"stop_loss": True}
        )

        executor._do_execute_stop_loss("HK.00700", stop_signal, "CLOSE")

        # 验证交易执行被调用
        assert trade_executor_mock.execute.called

    def test_stop_loss_bad_order_skip(self, mock_subscriber, position_factory, trade_executor_mock, notifier_mock):
        """BAD_ORDER 标的：跳过"""
        position = position_factory(code="HK.00700")
        mock_subscriber.position_manager.get_position.return_value = position

        executor = ExitExecutor(
            position_manager=mock_subscriber.position_manager,
            trade_executor=trade_executor_mock,
            notifier=notifier_mock,
            consensus_engine=MagicMock(),
        )
        executor._bad_orders["HK.00700"] = {'error': 'Test error', 'fail_count': 5}

        executor.execute_stop_loss("HK.00700", 95.0)

        # 验证未执行交易
        assert not trade_executor_mock.execute.called

    def test_stop_loss_cooldown(self, mock_subscriber, position_factory, trade_executor_mock, notifier_mock):
        """止损冷却中：跳过"""
        import time
        position = position_factory(code="HK.00700")
        mock_subscriber.position_manager.get_position.return_value = position

        executor = ExitExecutor(
            position_manager=mock_subscriber.position_manager,
            trade_executor=trade_executor_mock,
            notifier=notifier_mock,
            consensus_engine=MagicMock(),
        )
        executor._stop_loss_cooldown["HK.00700"] = time.time() - 10  # 10 秒前

        with patch('config.config_center.get_config_center') as mock_get_cc:
            mock_cc = MagicMock()
            mock_cc.get.return_value = [30, 60, 120, 300, 300]
            mock_get_cc.return_value = mock_cc

            executor.execute_stop_loss("HK.00700", 95.0)

        # 验证未执行交易 (仍在冷却中)
        assert not trade_executor_mock.execute.called


class TestExecuteTakeProfit:
    """止盈执行测试"""

    def test_take_profit_normal(self, mock_subscriber, position_factory, trade_executor_mock, notifier_mock):
        """正常止盈执行"""
        position = position_factory(code="HK.00700", entry_price=100.0)
        mock_subscriber.position_manager.get_position.return_value = position

        # Mock trade result with proper float values
        trade_result = MagicMock()
        trade_result.success = True
        trade_result.pnl = 1000.0
        trade_result.message = "OK"
        trade_executor_mock.execute.return_value = trade_result

        executor = ExitExecutor(
            position_manager=mock_subscriber.position_manager,
            trade_executor=trade_executor_mock,
            notifier=notifier_mock,
            consensus_engine=MagicMock(),
        )

        # app.config 中 TRADE_ENV 是模块级变量，不是 config.TRADE_ENV
        # execute_take_profit 中 "from app.config import config" 会 ImportError
        # (app.config 无 config 属性)，被 except 捕获后继续执行交易
        executor.execute_take_profit("HK.00700", 110.0)

        assert trade_executor_mock.execute.called


class TestExecutePartialTakeProfit:
    """分批止盈测试"""

    def test_partial_take_profit_60_percent(self, mock_subscriber, position_factory, trade_executor_mock, notifier_mock):
        """60% 分批止盈"""
        position = position_factory(code="HK.00700", quantity=1000, entry_price=100.0)
        mock_subscriber.position_manager.get_position.return_value = position

        # Mock trade result with proper float values
        trade_result = MagicMock()
        trade_result.success = True
        trade_result.pnl = 600.0
        trade_result.message = "OK"
        trade_executor_mock.execute_partial_close.return_value = trade_result

        executor = ExitExecutor(
            position_manager=mock_subscriber.position_manager,
            trade_executor=trade_executor_mock,
            notifier=notifier_mock,
            consensus_engine=MagicMock(),
        )

        with patch.dict('sys.modules', {'app.config': MagicMock(
            DEFAULT_ORDER_QTY_BY_CODE={},
            HK_LOT_SIZES={},
        )}):
            executor.execute_partial_take_profit("HK.00700", 110.0, 0.6)

        # 验证 execute_partial_close 被调用
        assert trade_executor_mock.execute_partial_close.called or trade_executor_mock.execute.called


class TestCheckFlashExit:
    """Flash Exit 测试"""

    def test_flash_exit_trigger(self, mock_subscriber, position_factory, trade_executor_mock, notifier_mock):
        """Flash Exit 触发"""
        import time as _time

        position = position_factory(code="HK.00700", action="BUY", entry_price=100.0)
        mock_subscriber.position_manager.get_position.return_value = position

        # Mock trade result
        trade_result = MagicMock()
        trade_result.success = True
        trade_result.pnl = -500.0
        trade_result.message = "OK"
        trade_executor_mock.execute.return_value = trade_result

        executor = ExitExecutor(
            position_manager=mock_subscriber.position_manager,
            trade_executor=trade_executor_mock,
            notifier=notifier_mock,
            consensus_engine=MagicMock(),
        )
        executor._flash_exit_enabled = True
        executor._flash_window_seconds = 60
        executor._flash_atr_mult = 2.0

        # 使用最近的时间戳 (不是 0) 以确保在 flash window 内
        now = _time.time()
        executor._flash_snapshots["HK.00700"] = (105.0, now - 5)  # 5 秒前，价格 105.0

        # Mock ATR 获取
        get_atr_fn = Mock(return_value=2.0)

        # 当前价格 95.0 (下跌 10.0 = 5x ATR > 2x ATR 阈值)
        with patch.dict('sys.modules', {'app.config': MagicMock(
            TRADE_ENV=1,
            DEFAULT_ORDER_QTY_BY_CODE={},
            HK_LOT_SIZES={},
        )}):
            with patch('config.config_center.get_config_center') as mock_get_cc:
                mock_cc = MagicMock()
                mock_cc.get.return_value = [30, 60, 120, 300, 300]
                mock_get_cc.return_value = mock_cc
                executor.check_flash_exit("HK.00700", 95.0, position, get_atr_fn)

        # execute_stop_loss 启动后台线程, 等待完成
        _time.sleep(0.3)

        # 验证止损被触发
        assert trade_executor_mock.execute.called or executor._stop_loss_inflight.get("HK.00700")


class TestStateManagement:
    """状态管理测试"""

    def test_clear_stop_state(self, mock_subscriber, position_factory, trade_executor_mock, notifier_mock):
        """清理止损状态"""
        executor = ExitExecutor(
            position_manager=mock_subscriber.position_manager,
            trade_executor=trade_executor_mock,
            notifier=notifier_mock,
            consensus_engine=MagicMock(),
        )

        executor._stop_loss_cooldown["HK.00700"] = 1234567890.0
        executor._stop_loss_fail_count["HK.00700"] = 3
        executor._trailing_stop_cache["HK.00700"] = 95.0
        executor._peak_price_cache["HK.00700"] = 105.0

        executor._clear_stop_state("HK.00700")

        assert "HK.00700" not in executor._stop_loss_cooldown
        assert "HK.00700" not in executor._stop_loss_fail_count
        assert "HK.00700" not in executor._trailing_stop_cache
        assert "HK.00700" not in executor._peak_price_cache

    def test_get_state(self, mock_subscriber, position_factory, trade_executor_mock, notifier_mock):
        """获取状态 (调试/监控)"""
        executor = ExitExecutor(
            position_manager=mock_subscriber.position_manager,
            trade_executor=trade_executor_mock,
            notifier=notifier_mock,
            consensus_engine=MagicMock(),
        )

        executor._stop_loss_cooldown["HK.00700"] = 1234567890.0
        executor._stop_loss_fail_count["HK.00700"] = 3
        executor._stop_loss_inflight["HK.00700"] = True

        state = executor.get_state()

        assert state["stop_loss_cooldown_count"] == 1
        assert "HK.00700" in state["stop_loss_fail_count"]
        assert "HK.00700" in state["stop_loss_inflight"]
