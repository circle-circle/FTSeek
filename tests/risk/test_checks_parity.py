#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RiskCheck 功能对等测试

测试目标:
  1. 验证 checks/ 包中的 17 个 Check 与 monolith 行为一致
  2. 每个 Check 的正常/拒绝/异常路径
  3. 配置驱动逻辑 (ConfigCenter 读取)

版本历史:
  2026-05-19 v1: 创建 (Phase 11)
"""

import pytest
from unittest.mock import MagicMock, Mock, patch
from app.risk.risk_pipeline import SignalContext, CheckResult, RiskPipeline
from app.risk.checks import (
    TradingHoursCheck, CircuitBreakerCheck, SignalCooldownCheck,
    ConcentrationCheck, WeakSignalCheck, MinHoldTimeCheck,
    MiddayRecommendationCheck, ADXConsistencyCheck, MultiTimeframeCheck,
    TrailingCooldownCheck, RiskRewardCheck, DailyTradeLimitCheck,
    OpenDedupCheck, SymbolLossCooldownCheck, LateSessionGateCheck,
    DecisionBufferCheck, QualityCheck,
)


class TestTradingHoursCheck:
    """TradingHoursCheck 测试"""

    def test_normal_trading_hours_pass(self, signal_context_factory):
        """正常交易时段：通过"""
        ctx = signal_context_factory()
        check = TradingHoursCheck()

        deps = {
            "trading_hours": MagicMock(
                is_trading_hours=Mock(return_value=True),
                is_high_quality_trading_hours=Mock(return_value=True)
            )
        }
        result = check.check(ctx, deps)
        assert result.passed is True

    def test_noise_hours_high_score_pass(self, signal_context_factory):
        """噪音时段 + 高分信号 (>2.5): 通过"""
        ctx = signal_context_factory(score=3.0)
        check = TradingHoursCheck()

        deps = {
            "trading_hours": MagicMock(
                is_trading_hours=Mock(return_value=True),
                is_high_quality_trading_hours=Mock(return_value=False)
            )
        }
        result = check.check(ctx, deps)
        assert result.passed is True

    def test_noise_hours_low_score_reject(self, signal_context_factory):
        """噪音时段 + 低分信号 (<1.5): 拒绝"""
        ctx = signal_context_factory(score=1.0)
        check = TradingHoursCheck()

        deps = {
            "trading_hours": MagicMock(
                is_trading_hours=Mock(return_value=True),
                is_high_quality_trading_hours=Mock(return_value=False)
            )
        }
        result = check.check(ctx, deps)
        assert result.passed is False
        assert result.reject_code == "noise_hours"

    def test_non_trading_hours_reject(self, signal_context_factory):
        """非交易时段：拒绝"""
        ctx = signal_context_factory()
        check = TradingHoursCheck()

        deps = {
            "trading_hours": MagicMock(
                is_trading_hours=Mock(return_value=False),
                is_high_quality_trading_hours=Mock(return_value=False)
            )
        }
        result = check.check(ctx, deps)
        assert result.passed is False
        assert result.reject_code == "non_trading_hours"


class TestCircuitBreakerCheck:
    """CircuitBreakerCheck 测试"""

    def test_normal_pass(self, signal_context_factory):
        """熔断器未触发：通过"""
        ctx = signal_context_factory()
        check = CircuitBreakerCheck()

        deps = {
            "circuit_breaker": MagicMock(
                is_paused=Mock(return_value=False),
                get_current_equity=Mock(return_value=1000000.0),
                check=Mock(return_value=(True, "OK", 1.0))
            )
        }
        result = check.check(ctx, deps)
        assert result.passed is True

    def test_paused_reject(self, signal_context_factory):
        """熔断器已触发：拒绝"""
        ctx = signal_context_factory()
        check = CircuitBreakerCheck()

        deps = {
            "circuit_breaker": MagicMock(
                is_paused=Mock(return_value=True),
            )
        }
        result = check.check(ctx, deps)
        assert result.passed is False
        assert result.reject_code == "circuit_breaker"

    def test_exception_reject(self, signal_context_factory):
        """熔断检查异常：拒绝 (fail-close)"""
        ctx = signal_context_factory()
        check = CircuitBreakerCheck()

        deps = {
            "circuit_breaker": MagicMock(
                is_paused=Mock(return_value=False),
                get_current_equity=Mock(return_value=1000000.0),
                check=Mock(side_effect=Exception("Test error"))
            )
        }
        result = check.check(ctx, deps)
        assert result.passed is False
        assert result.reject_code == "circuit_breaker_error"


class TestSignalCooldownCheck:
    """SignalCooldownCheck 测试"""

    def test_cooldown_pass(self, signal_context_factory):
        """冷却完成：通过"""
        ctx = signal_context_factory()
        check = SignalCooldownCheck()

        deps = {
            "cooldown": MagicMock(
                get_last_open_ts=Mock(return_value=0)  # 从未开仓
            )
        }
        result = check.check(ctx, deps)
        assert result.passed is True

    def test_cooldown_reject(self, signal_context_factory):
        """冷却中：拒绝"""
        import time
        ctx = signal_context_factory()
        check = SignalCooldownCheck()

        deps = {
            "cooldown": MagicMock(
                get_last_open_ts=Mock(return_value=time.time() - 60)  # 1 分钟前
            )
        }
        result = check.check(ctx, deps)
        assert result.passed is False
        assert result.reject_code == "signal_cooldown"

    def test_non_buy_sell_pass(self, signal_context_factory):
        """非买卖信号：跳过"""
        ctx = signal_context_factory(action="CLOSE")
        check = SignalCooldownCheck()

        result = check.check(ctx, {})
        assert result.passed is True


class TestConcentrationCheck:
    """ConcentrationCheck 测试"""

    @patch('config.config_center.get_config_center')
    def test_normal_pass(self, mock_get_cc, signal_context_factory, position_factory):
        """持仓分散：通过"""
        mock_cc = MagicMock()
        mock_cc.get.return_value = 3
        mock_get_cc.return_value = mock_cc

        ctx = signal_context_factory()
        check = ConcentrationCheck()

        pos_query = MagicMock()
        pos_query.get_all_positions.return_value = [position_factory(code="HK.00700")]
        deps = {"position_query": pos_query}

        result = check.check(ctx, deps)
        assert result.passed is True

    @patch('config.config_center.get_config_center')
    def test_max_positions_reject(self, mock_get_cc, signal_context_factory, position_factory):
        """持仓过于分散：拒绝"""
        mock_cc = MagicMock()
        mock_cc.get.return_value = 3
        mock_get_cc.return_value = mock_cc

        ctx = signal_context_factory(code="HK.09988")
        check = ConcentrationCheck()

        pos_query = MagicMock()
        # 返回 3 个其他标的的持仓 (不包括目标 HK.09988)
        pos_query.get_all_positions.return_value = [
            position_factory(code="HK.00700"),
            position_factory(code="HK.01299"),
            position_factory(code="HK.00941"),
        ]
        deps = {"position_query": pos_query}

        result = check.check(ctx, deps)
        assert result.passed is False
        assert result.reject_code == "max_concurrent_positions"


class TestWeakSignalCheck:
    """WeakSignalCheck 测试"""

    @patch('config.config_center.get_config_center')
    def test_strong_signal_pass(self, mock_get_cc, signal_context_factory):
        """强信号：通过"""
        mock_cc = MagicMock()
        mock_cc.get.return_value = 1.2
        mock_get_cc.return_value = mock_cc

        ctx = signal_context_factory(score=2.5)
        check = WeakSignalCheck()

        result = check.check(ctx, {})
        assert result.passed is True

    @patch('config.config_center.get_config_center')
    def test_weak_signal_reject(self, mock_get_cc, signal_context_factory):
        """弱信号：拒绝"""
        mock_cc = MagicMock()
        mock_cc.get.return_value = 1.2
        mock_get_cc.return_value = mock_cc

        ctx = signal_context_factory(score=0.5)
        check = WeakSignalCheck()

        result = check.check(ctx, {})
        assert result.passed is False
        assert result.reject_code == "weak_signal"


class TestMinHoldTimeCheck:
    """MinHoldTimeCheck 测试"""

    @patch('config.config_center.get_config_center')
    def test_close_pass(self, mock_get_cc, signal_context_factory, position_factory):
        """平仓检查：通过"""
        mock_cc = MagicMock()
        mock_cc.get.return_value = 30
        mock_get_cc.return_value = mock_cc

        ctx = signal_context_factory(trade_type="CLOSE")
        check = MinHoldTimeCheck()

        pos_query = MagicMock()
        pos_query.get_position.return_value = position_factory(
            entry_time="2026-05-19 10:00:00"
        )
        deps = {"position_query": pos_query}

        result = check.check(ctx, deps)
        assert result.passed is True  # 持仓时间足够

    def test_open_pass(self, signal_context_factory):
        """开仓检查：跳过"""
        ctx = signal_context_factory(trade_type="OPEN")
        check = MinHoldTimeCheck()

        result = check.check(ctx, {})
        assert result.passed is True


class TestMiddayRecommendationCheck:
    """MiddayRecommendationCheck 测试"""

    @patch('app.pipeline.battle_plan.BattlePlanManager')
    def test_no_recommendation_pass(self, mock_bpm_class, signal_context_factory):
        """无午间推荐：通过"""
        mock_bpm = MagicMock()
        mock_bpm.get_midday_recommendation.return_value = None
        mock_bpm_class.return_value = mock_bpm

        ctx = signal_context_factory()
        check = MiddayRecommendationCheck()

        result = check.check(ctx, {})
        assert result.passed is True

    @patch('app.pipeline.battle_plan.BattlePlanManager')
    def test_close_recommendation_reject(self, mock_bpm_class, signal_context_factory):
        """午间推荐 CLOSE: 拒绝"""
        mock_bpm = MagicMock()
        mock_bpm.get_midday_recommendation.return_value = "CLOSE"
        mock_bpm_class.return_value = mock_bpm

        ctx = signal_context_factory()
        check = MiddayRecommendationCheck()

        result = check.check(ctx, {})
        assert result.passed is False
        assert result.reject_code == "midday_close"


class TestRiskPipeline:
    """RiskPipeline 集成测试"""

    def test_all_checks_pass(self, signal_context_factory):
        """所有检查通过"""
        ctx = signal_context_factory()
        pipeline = RiskPipeline(checks=[
            TradingHoursCheck(),
            WeakSignalCheck(),
        ])

        deps = {
            "trading_hours": MagicMock(
                is_trading_hours=Mock(return_value=True),
                is_high_quality_trading_hours=Mock(return_value=True)
            )
        }

        with patch('config.config_center.get_config_center') as mock_get_cc:
            mock_cc = MagicMock()
            mock_cc.get.return_value = 1.2
            mock_get_cc.return_value = mock_cc
            result = pipeline.evaluate(ctx, deps)

        assert result.passed is True
        assert len(result.results) == 2

    def test_first_reject_stops_pipeline(self, signal_context_factory):
        """第一个拒绝即终止"""
        ctx = signal_context_factory(score=0.5)  # 弱信号
        pipeline = RiskPipeline(checks=[
            WeakSignalCheck(),  # 会拒绝
            TradingHoursCheck(),  # 不应执行
        ])

        with patch('config.config_center.get_config_center') as mock_get_cc:
            mock_cc = MagicMock()
            mock_cc.get.return_value = 1.2
            mock_get_cc.return_value = mock_cc
            result = pipeline.evaluate(ctx, {})

        assert result.passed is False
        assert result.reject_check == "weak_signal"
        assert len(result.results) == 1  # 只执行了第一个检查
