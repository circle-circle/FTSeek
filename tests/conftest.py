#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
共享测试 fixtures

提供所有测试模块共用的 mock 对象和 fixtures。

版本历史:
  2026-05-19 v1: 创建共享 fixtures (Phase 11)
"""

import pytest
from unittest.mock import MagicMock, Mock
from datetime import datetime
from dataclasses import dataclass
from typing import Dict, List, Optional, Any


# ============================================================
#  Mock DataSubscriber (God Class)
# ============================================================

@pytest.fixture
def mock_subscriber():
    """创建 Mock DataSubscriber 实例"""
    sub = MagicMock()

    # 交易时段
    sub._is_trading_hours.return_value = True
    sub._is_high_quality_trading_hours.return_value = True

    # 冷却状态
    sub._trailing_exit_time = {}
    sub._trailing_stop_cache = {}
    sub._peak_price_cache = {}
    sub._open_position_ts = {}
    sub._symbol_loss_cooldowns = {}
    sub._stop_loss_cooldown = {}
    sub._stop_loss_fail_count = {}
    sub._stop_loss_inflight = {}
    sub._bad_orders = {}

    # 熔断器
    sub.circuit_breaker = MagicMock()
    sub.circuit_breaker.is_paused.return_value = False
    sub.circuit_breaker.check.return_value = (True, "OK", 1.0)

    # 持仓管理器
    sub.position_manager = MagicMock()
    sub.position_manager.get_position.return_value = None
    sub.position_manager.get_all_positions.return_value = []

    # 质量评分
    sub.quality_scorer = None
    sub._check_quality_and_valuation.return_value = None

    # 市场数据
    sub._get_multi_timeframe_signal.return_value = (True, 0.0, "test")
    sub._get_mtf_alignment_score.return_value = (0.5, "test")
    sub._consensus = MagicMock()
    sub._consensus._adx_cache = {}
    sub._consensus.update_mtf_alignment = Mock()

    # 账户权益
    sub.get_current_equity.return_value = 1000000.0

    # 场景缓存
    sub._scenario_cache = {}

    return sub


# ============================================================
#  SignalContext Factory
# ============================================================

@dataclass
class MockConsensusResult:
    """Mock ConsensusResult"""
    code: str
    action: str
    score: float = 2.0
    confidence: float = 0.8
    price: float = 100.0
    market_regime: str = "trending"
    adx_value: float = 25.0
    agree_strategies: List[str] = None
    votes: List[Any] = None
    weight_details: Dict[str, Any] = None

    def __post_init__(self):
        if self.agree_strategies is None:
            self.agree_strategies = ["confluence_v2"]
        if self.votes is None:
            self.votes = []
        if self.weight_details is None:
            self.weight_details = {}


@pytest.fixture
def signal_context_factory():
    """创建 SignalContext 工厂"""
    def make_ctx(code="HK.00700", action="BUY", score=2.0, confidence=0.8,
                 price=100.0, trade_type="OPEN"):
        from app.risk.risk_pipeline import SignalContext

        consensus = MockConsensusResult(
            code=code, action=action, score=score,
            confidence=confidence, price=price
        )

        ctx = SignalContext(
            consensus_result=consensus,
            trace_id="test_trace_001",
            trade_type=trade_type,
        )
        return ctx

    return make_ctx


# ============================================================
#  CheckResult 工厂
# ============================================================

@pytest.fixture
def check_result_factory():
    """创建 CheckResult 工厂"""
    def make_result(passed=True, check_name="test_check", reason="",
                    reject_code="", context_updates=None):
        from app.risk.risk_pipeline import CheckResult
        return CheckResult(
            passed=passed,
            check_name=check_name,
            reason=reason,
            reject_code=reject_code,
            context_updates=context_updates or {}
        )

    return make_result


# ============================================================
#  Position Mock
# ============================================================

@dataclass
class MockPosition:
    """Mock Position"""
    code: str
    action: str = "BUY"
    quantity: int = 100
    entry_price: float = 100.0
    stop_loss: float = 95.0
    take_profit: float = 110.0
    entry_time: str = "2026-05-19 10:00:00"
    pnl: Optional[float] = None
    highest_price: Optional[float] = None
    lowest_price: Optional[float] = None
    signal_id: Optional[str] = None


@pytest.fixture
def position_factory():
    """创建 Position 工厂"""
    def make_pos(code="HK.00700", action="BUY", quantity=100, entry_price=100.0,
                 stop_loss=95.0, take_profit=110.0, entry_time="2026-05-19 10:00:00"):
        return MockPosition(
            code=code, action=action, quantity=quantity,
            entry_price=entry_price, stop_loss=stop_loss,
            take_profit=take_profit, entry_time=entry_time
        )

    return make_pos


# ============================================================
#  ConfigCenter Mock
# ============================================================

@pytest.fixture
def mock_config_center(monkeypatch):
    """Mock ConfigCenter"""
    mock_cc = MagicMock()
    mock_cc.get.side_effect = lambda key, default=None: {
        'risk_limits.max_positions': 3,
        'risk_limits.max_daily_trades': 4,
        'risk_limits.min_rr': 1.25,
        'risk_limits.weak_signal_min_score': 1.2,
        'risk_limits.trailing_cooldown_sec': 900,
        'risk_limits.min_hold_minutes': 30,
        'risk_limits.stop_loss_cooldown_schedule': [30, 60, 120, 300, 300],
        'risk_limits.max_symbol_trades_per_day': 3,
        'risk_limits.min_open_interval_sec': 300,
        'risk_limits.min_open_interval_mode': 'warn',
    }.get(key, default)

    monkeypatch.setattr(
        'config.config_center.get_config_center',
        lambda: mock_cc
    )
    return mock_cc


# ============================================================
#  TradeExecutor Mock
# ============================================================

@dataclass
class MockTradeResult:
    """Mock TradeResult"""
    success: bool = True
    message: str = "OK"
    order_id: str = "test_order_001"
    price: float = 100.0
    quantity: int = 100
    pnl: Optional[float] = None
    action: str = "BUY"


@pytest.fixture
def trade_executor_mock():
    """创建 TradeExecutor mock"""
    executor = MagicMock()
    executor.execute.return_value = MockTradeResult()
    executor.execute_partial_close.return_value = MockTradeResult()
    return executor


# ============================================================
#  Notifier Mock
# ============================================================

@pytest.fixture
def notifier_mock():
    """创建 Notifier mock"""
    notifier = MagicMock()
    notifier.send_open_position_message.return_value = True
    notifier.send_close_position_message.return_value = True
    return notifier


# ============================================================
#  SignalTracker Mock
# ============================================================

@pytest.fixture
def signal_tracker_mock():
    """创建 SignalTracker mock"""
    tracker = MagicMock()
    tracker.record_signal.return_value = "signal_001"
    tracker.record_execution.return_value = True
    tracker.record_exit.return_value = True
    tracker.record_exit_by_code.return_value = True
    return tracker
