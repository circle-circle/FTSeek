#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ConsensusExecutor 单元测试

测试目标:
  1. 共识结果执行逻辑 (交易 + 推送 + 领域事件)
  2. 开仓去重/开仓间隔/日内频次监控
  3. 动态止损止盈写入
  4. 冷却时间更新

版本历史:
  2026-05-19 v1: 创建 (Phase 11)
"""

import pytest
from unittest.mock import MagicMock, Mock, patch
from app.services.consensus_executor import ConsensusExecutor


class TestExecuteConsensus:
    """共识执行测试"""

    def test_execute_open_normal(
        self, mock_subscriber, position_factory, trade_executor_mock,
        notifier_mock, signal_tracker_mock
    ):
        """正常开仓执行"""
        executor = ConsensusExecutor(
            trade_executor=trade_executor_mock,
            notifier=notifier_mock,
            position_manager=mock_subscriber.position_manager,
            signal_tracker=signal_tracker_mock,
            evaluator=MagicMock(),
            circuit_breaker=MagicMock(),
            adaptive_weight_manager=MagicMock(),
            consensus_engine=MagicMock(),
        )

        # Mock 信号
        consensus_signal = MagicMock()
        consensus_signal.code = "HK.00700"
        consensus_signal.price = 100.0
        consensus_signal.quantity = 100

        # Mock 共识结果
        result = MagicMock()
        result.code = "HK.00700"
        result.action = MagicMock()
        result.action.value = "BUY"
        result.score = 2.5
        result.confidence = 0.85
        result.agree_strategies = ["confluence_v2"]
        result.market_regime = "trending"

        trade_result = executor.execute_consensus(
            code="HK.00700",
            consensus_signal=consensus_signal,
            result=result,
            position=None,
            trade_type="OPEN",
            signal_id="signal_001",
            strategies_str="confluence_v2",
        )

        assert trade_result is not None
        assert trade_executor_mock.execute.called
        assert notifier_mock.send_open_position_message.called

    def test_execute_close_normal(
        self, mock_subscriber, position_factory, trade_executor_mock,
        notifier_mock, signal_tracker_mock
    ):
        """正常平仓执行"""
        position = position_factory(code="HK.00700", entry_price=100.0)
        mock_subscriber.position_manager.get_position.return_value = position

        executor = ConsensusExecutor(
            trade_executor=trade_executor_mock,
            notifier=notifier_mock,
            position_manager=mock_subscriber.position_manager,
            signal_tracker=signal_tracker_mock,
            evaluator=MagicMock(),
            circuit_breaker=MagicMock(),
            adaptive_weight_manager=MagicMock(),
            consensus_engine=MagicMock(),
        )

        consensus_signal = MagicMock()
        consensus_signal.code = "HK.00700"
        consensus_signal.price = 110.0
        consensus_signal.quantity = 100

        result = MagicMock()
        result.code = "HK.00700"
        result.action.value = "SELL"
        result.score = 2.5

        trade_result = executor.execute_consensus(
            code="HK.00700",
            consensus_signal=consensus_signal,
            result=result,
            position=position,
            trade_type="CLOSE",
            signal_id="signal_001",
            strategies_str="confluence_v2",
        )

        assert trade_result is not None
        assert notifier_mock.send_close_position_message.called


class TestOpenDedup:
    """开仓去重测试"""

    def test_open_dedup_pass(self, mock_subscriber, trade_executor_mock, notifier_mock, signal_tracker_mock):
        """开仓去重：通过"""
        executor = ConsensusExecutor(
            trade_executor=trade_executor_mock,
            notifier=notifier_mock,
            position_manager=mock_subscriber.position_manager,
            signal_tracker=signal_tracker_mock,
            evaluator=MagicMock(),
            circuit_breaker=MagicMock(),
            adaptive_weight_manager=MagicMock(),
            consensus_engine=MagicMock(),
        )

        result = executor.check_open_dedup("HK.00700", "signal_001")
        assert result is True
        assert "HK.00700" in executor._open_position_ts

    def test_open_dedup_reject(self, mock_subscriber, trade_executor_mock, notifier_mock, signal_tracker_mock):
        """开仓去重：拒绝 (5 秒内重复)"""
        import time
        executor = ConsensusExecutor(
            trade_executor=trade_executor_mock,
            notifier=notifier_mock,
            position_manager=mock_subscriber.position_manager,
            signal_tracker=signal_tracker_mock,
            evaluator=MagicMock(),
            circuit_breaker=MagicMock(),
            adaptive_weight_manager=MagicMock(),
            consensus_engine=MagicMock(),
        )
        executor._open_position_ts["HK.00700"] = time.time() - 1  # 1 秒前

        result = executor.check_open_dedup("HK.00700", "signal_001")
        assert result is False
        assert signal_tracker_mock.record_execution.called


class TestOpenInterval:
    """开仓间隔测试"""

    @patch('config.config_center.get_config_center')
    def test_open_interval_pass(self, mock_get_cc, mock_subscriber, trade_executor_mock, notifier_mock, signal_tracker_mock):
        """开仓间隔：通过"""
        mock_cc = MagicMock()
        mock_cc.get.side_effect = lambda key, default=None: {
            'risk_limits.min_open_interval_sec': 300,
            'risk_limits.min_open_interval_mode': 'warn',
        }.get(key, default)
        mock_get_cc.return_value = mock_cc

        executor = ConsensusExecutor(
            trade_executor=trade_executor_mock,
            notifier=notifier_mock,
            position_manager=mock_subscriber.position_manager,
            signal_tracker=signal_tracker_mock,
            evaluator=MagicMock(),
            circuit_breaker=MagicMock(),
            adaptive_weight_manager=MagicMock(),
            consensus_engine=MagicMock(),
        )
        executor._last_global_open_ts = 0  # 从未开仓

        result = executor.check_open_interval("HK.00700", "signal_001")
        assert result is True

    @patch('config.config_center.get_config_center')
    def test_open_interval_block(self, mock_get_cc, mock_subscriber, trade_executor_mock, notifier_mock, signal_tracker_mock):
        """开仓间隔：拒绝 (block 模式)"""
        import time
        mock_cc = MagicMock()
        mock_cc.get.side_effect = lambda key, default=None: {
            'risk_limits': {
                'min_open_interval_sec': 300,
                'min_open_interval_mode': 'block',
            },
        }.get(key, default)
        mock_get_cc.return_value = mock_cc

        executor = ConsensusExecutor(
            trade_executor=trade_executor_mock,
            notifier=notifier_mock,
            position_manager=mock_subscriber.position_manager,
            signal_tracker=signal_tracker_mock,
            evaluator=MagicMock(),
            circuit_breaker=MagicMock(),
            adaptive_weight_manager=MagicMock(),
            consensus_engine=MagicMock(),
        )
        executor._last_global_open_ts = time.time() - 60  # 1 分钟前

        result = executor.check_open_interval("HK.00700", "signal_001")
        assert result is False


class TestSymbolTrades:
    """标的日内交易次数测试"""

    @patch('config.config_center.get_config_center')
    def test_symbol_trades_under_limit(self, mock_get_cc, mock_subscriber, trade_executor_mock, notifier_mock, signal_tracker_mock):
        """标的交易次数未超限：通过"""
        mock_cc = MagicMock()
        mock_cc.get.return_value = 3
        mock_get_cc.return_value = mock_cc

        mock_subscriber.position_manager.get_today_trade_count.return_value = 1

        executor = ConsensusExecutor(
            trade_executor=trade_executor_mock,
            notifier=notifier_mock,
            position_manager=mock_subscriber.position_manager,
            signal_tracker=signal_tracker_mock,
            evaluator=MagicMock(),
            circuit_breaker=MagicMock(),
            adaptive_weight_manager=MagicMock(),
            consensus_engine=MagicMock(),
        )

        result = executor.check_symbol_trades("HK.00700")
        assert result is True

    @patch('config.config_center.get_config_center')
    def test_symbol_trades_over_limit_warn(self, mock_get_cc, mock_subscriber, trade_executor_mock, notifier_mock, signal_tracker_mock):
        """标的交易次数超限：警告但通过 (模拟盘)"""
        mock_cc = MagicMock()
        mock_cc.get.return_value = 3
        mock_get_cc.return_value = mock_cc

        mock_subscriber.position_manager.get_today_trade_count.return_value = 5

        executor = ConsensusExecutor(
            trade_executor=trade_executor_mock,
            notifier=notifier_mock,
            position_manager=mock_subscriber.position_manager,
            signal_tracker=signal_tracker_mock,
            evaluator=MagicMock(),
            circuit_breaker=MagicMock(),
            adaptive_weight_manager=MagicMock(),
            consensus_engine=MagicMock(),
        )

        result = executor.check_symbol_trades("HK.00700")
        assert result is True  # 模拟盘放行


class TestUpdateCooldown:
    """冷却时间更新测试"""

    def test_update_cooldown(self, mock_subscriber, trade_executor_mock, notifier_mock, signal_tracker_mock):
        """更新冷却时间"""
        executor = ConsensusExecutor(
            trade_executor=trade_executor_mock,
            notifier=notifier_mock,
            position_manager=mock_subscriber.position_manager,
            signal_tracker=signal_tracker_mock,
            evaluator=MagicMock(),
            circuit_breaker=MagicMock(),
            adaptive_weight_manager=MagicMock(),
            consensus_engine=MagicMock(),
        )

        trade_result = MagicMock()
        trade_result.pnl = 500.0

        executor._update_cooldown("HK.00700", trade_result)

        assert "HK.00700" in executor._cooldown
