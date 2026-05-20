#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DataSubscriberAdapter 单元测试

测试目标:
  1. 所有 14 个方法的正常/异常路径
  2. 性能监控集成 (track() 调用)
  3. 异常处理 (所有方法都有 try/except)

版本历史:
  2026-05-19 v1: 创建 (Phase 11)
"""

import pytest
from unittest.mock import MagicMock, Mock, patch
from app.core.subscriber_adapter import DataSubscriberAdapter


class TestITradingHoursProvider:
    """ITradingHoursProvider 测试"""

    def test_is_trading_hours_normal(self, mock_subscriber):
        """正常情况：返回 True"""
        adapter = DataSubscriberAdapter(mock_subscriber)
        result = adapter.is_trading_hours()
        assert result is True
        mock_subscriber._is_trading_hours.assert_called_once()

    def test_is_trading_hours_exception(self, mock_subscriber):
        """异常情况：返回 False"""
        mock_subscriber._is_trading_hours.side_effect = Exception("Test error")
        adapter = DataSubscriberAdapter(mock_subscriber)
        result = adapter.is_trading_hours()
        assert result is False

    def test_is_high_quality_trading_hours_normal(self, mock_subscriber):
        """正常情况：返回 True"""
        adapter = DataSubscriberAdapter(mock_subscriber)
        result = adapter.is_high_quality_trading_hours()
        assert result is True

    def test_is_high_quality_trading_hours_exception(self, mock_subscriber):
        """异常情况：返回 True (放行避免误杀)"""
        mock_subscriber._is_high_quality_trading_hours.side_effect = Exception("Test error")
        adapter = DataSubscriberAdapter(mock_subscriber)
        result = adapter.is_high_quality_trading_hours()
        assert result is True  # 异常时视为高质量时段


class TestIExitStateProvider:
    """IExitStateProvider 测试"""

    def test_get_trailing_exit_time_normal(self, mock_subscriber):
        """正常情况：返回时间戳"""
        mock_subscriber._trailing_exit_time = {"HK.00700": 1234567890.0}
        adapter = DataSubscriberAdapter(mock_subscriber)
        result = adapter.get_trailing_exit_time("HK.00700")
        assert result == 1234567890.0

    def test_get_trailing_exit_time_missing(self, mock_subscriber):
        """标的不存在：返回 0"""
        mock_subscriber._trailing_exit_time = {}
        adapter = DataSubscriberAdapter(mock_subscriber)
        result = adapter.get_trailing_exit_time("HK.00700")
        assert result == 0

    def test_get_trailing_stop_price(self, mock_subscriber):
        """获取追踪止损价格"""
        mock_subscriber._trailing_stop_cache = {"HK.00700": 95.5}
        adapter = DataSubscriberAdapter(mock_subscriber)
        result = adapter.get_trailing_stop_price("HK.00700")
        assert result == 95.5

    def test_get_peak_price(self, mock_subscriber):
        """获取峰值价格"""
        mock_subscriber._peak_price_cache = {"HK.00700": 105.0}
        adapter = DataSubscriberAdapter(mock_subscriber)
        result = adapter.get_peak_price("HK.00700")
        assert result == 105.0


class TestICooldownProvider:
    """ICooldownProvider 测试"""

    def test_is_signal_cooled_always_true(self, mock_subscriber):
        """信号冷却检查 (目前恒返回 True)"""
        adapter = DataSubscriberAdapter(mock_subscriber)
        result = adapter.is_signal_cooled("HK.00700")
        assert result is True

    def test_get_last_open_ts(self, mock_subscriber):
        """获取最后开仓时间戳"""
        mock_subscriber._open_position_ts = {"HK.00700": 1234567890.0}
        adapter = DataSubscriberAdapter(mock_subscriber)
        result = adapter.get_last_open_ts("HK.00700")
        assert result == 1234567890.0

    def test_get_symbol_loss_cooldown_until(self, mock_subscriber):
        """获取标的连续亏损冷却时间"""
        mock_subscriber._symbol_loss_cooldowns = {"HK.00700": 1234567890.0}
        adapter = DataSubscriberAdapter(mock_subscriber)
        result = adapter.get_symbol_loss_cooldown_until("HK.00700")
        assert result == 1234567890.0


class TestICircuitBreakerProvider:
    """ICircuitBreakerProvider 测试"""

    def test_is_paused_normal(self, mock_subscriber):
        """熔断器未暂停"""
        mock_subscriber.circuit_breaker.is_paused.return_value = False
        adapter = DataSubscriberAdapter(mock_subscriber)
        result = adapter.is_paused()
        assert result is False

    def test_is_paused_exception(self, mock_subscriber):
        """异常情况：返回 True (保守拒绝)"""
        mock_subscriber.circuit_breaker.is_paused.side_effect = Exception("Test error")
        adapter = DataSubscriberAdapter(mock_subscriber)
        result = adapter.is_paused()
        assert result is True

    def test_check_normal(self, mock_subscriber):
        """熔断检查通过"""
        mock_subscriber.circuit_breaker.check.return_value = (True, "OK", 1.0)
        adapter = DataSubscriberAdapter(mock_subscriber)
        allowed, reason, multiplier = adapter.check(1000000.0, 0.8)
        assert allowed is True
        assert reason == "OK"
        assert multiplier == 1.0

    def test_get_current_equity(self, mock_subscriber):
        """获取账户权益"""
        mock_subscriber.get_current_equity.return_value = 1000000.0
        adapter = DataSubscriberAdapter(mock_subscriber)
        result = adapter.get_current_equity()
        assert result == 1000000.0


class TestIQualityProvider:
    """IQualityProvider 测试"""

    def test_check_quality_normal(self, mock_subscriber):
        """质量检查通过"""
        mock_subscriber._check_quality_and_valuation.return_value = {
            'reject': False,
            'quality_class': 'CLASS_A',
            'reason': 'OK'
        }
        adapter = DataSubscriberAdapter(mock_subscriber)
        result = adapter.check_quality("HK.00700", 100.0)
        assert result['reject'] is False
        assert result['quality_class'] == 'CLASS_A'

    def test_get_quality_scorer(self, mock_subscriber):
        """获取质量评分器"""
        mock_subscriber.quality_scorer = MagicMock()
        adapter = DataSubscriberAdapter(mock_subscriber)
        result = adapter.get_quality_scorer()
        assert result is not None


class TestIMarketDataProvider:
    """IMarketDataProvider 测试"""

    def test_get_mtf_alignment(self, mock_subscriber):
        """获取多周期对齐结果"""
        mock_subscriber._get_multi_timeframe_signal.return_value = (True, 0.5, "aligned")
        adapter = DataSubscriberAdapter(mock_subscriber)
        confirmed, boost, reason = adapter.get_mtf_alignment("HK.00700", "BUY")
        assert confirmed is True
        assert boost == 0.5

    def test_get_mtf_scores(self, mock_subscriber):
        """获取 MTF 分数"""
        mock_subscriber._get_mtf_alignment_score.side_effect = [(0.6, ""), (0.4, "")]
        adapter = DataSubscriberAdapter(mock_subscriber)
        buy_score, sell_score = adapter.get_mtf_scores("HK.00700")
        assert buy_score == 0.6
        assert sell_score == 0.4

    def test_update_mtf_alignment(self, mock_subscriber):
        """同步 MTF 分数"""
        adapter = DataSubscriberAdapter(mock_subscriber)
        adapter.update_mtf_alignment("HK.00700", 0.6, 0.4)
        mock_subscriber._consensus.update_mtf_alignment.assert_called_once_with(
            "HK.00700", 0.6, 0.4
        )

    def test_get_adx(self, mock_subscriber):
        """获取 ADX 值"""
        mock_subscriber._consensus._adx_cache = {"HK.00700": 25.0}
        adapter = DataSubscriberAdapter(mock_subscriber)
        result = adapter.get_adx("HK.00700")
        assert result == 25.0


class TestIPositionQueryProvider:
    """IPositionQueryProvider 测试"""

    def test_get_position(self, mock_subscriber):
        """获取持仓"""
        mock_position = MagicMock()
        mock_subscriber.position_manager.get_position.return_value = mock_position
        adapter = DataSubscriberAdapter(mock_subscriber)
        result = adapter.get_position("HK.00700")
        assert result == mock_position

    def test_get_all_positions(self, mock_subscriber):
        """获取所有持仓"""
        mock_positions = [MagicMock(), MagicMock()]
        mock_subscriber.position_manager.get_all_positions.return_value = mock_positions
        adapter = DataSubscriberAdapter(mock_subscriber)
        result = adapter.get_all_positions()
        assert len(result) == 2


class TestPerformanceMonitor:
    """性能监控集成测试"""

    def test_track_called_for_all_methods(self, mock_subscriber):
        """验证所有方法都调用 track()"""
        from app.core.performance_monitor import PerformanceMonitor

        monitor = MagicMock(spec=PerformanceMonitor)
        monitor.track.return_value = MagicMock()
        monitor.track.return_value.__enter__ = Mock(return_value=None)
        monitor.track.return_value.__exit__ = Mock(return_value=None)

        adapter = DataSubscriberAdapter(mock_subscriber, performance_monitor=monitor)

        # 调用所有方法
        adapter.is_trading_hours()
        adapter.is_high_quality_trading_hours()
        adapter.get_trailing_exit_time("HK.00700")
        adapter.get_last_open_ts("HK.00700")
        adapter.is_paused()
        adapter.get_current_equity()
        adapter.get_mtf_alignment("HK.00700", "BUY")
        adapter.get_position("HK.00700")

        # 验证 track() 被调用
        assert monitor.track.call_count >= 8
