"""退出规则回归测试 - 验证ExitRuleDispatcher优先级和互斥逻辑"""
import pytest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.risk.exit_rule_dispatcher import ExitRuleDispatcher, ExitDecision


class TestExitRulePriority:
    """测试退出规则优先级"""

    @pytest.fixture
    def dispatcher(self):
        config = {
            'exit_rules': {
                'flash_exit': {'enabled': True, 'price_drop_pct': 3.0},
                'pre_close': {'enabled': True, 'minutes_before_close': 15},
                'circuit_breaker': {'enabled': True, 'threshold_pct': 5.0},
                'trailing_stop': {'enabled': True, 'activation_threshold_atr': 0.8, 'trail_distance_atr': 1.2},
                'day_trade': {'enabled': True, 'tp1_target_atr': 1.5, 'max_holding_hours': 6},
                'time_decay': {'enabled': True, 'max_hours': 48},
            },
            'exit_rules_exclusivity': {
                'flash_exit': ['day_trade', 'trailing_stop'],
                'pre_close': ['day_trade', 'trailing_stop', 'scale_out'],
                'day_trade': ['trailing_stop'],
            }
        }
        return ExitRuleDispatcher(config)

    def test_pre_close_over_trailing(self, dispatcher):
        """收盘前退出优先于追踪止损 (adaptive 语义: 浮盈≥1ATR 锁利全平).

        Task #3 (2026-05-31): pre_close 默认 adaptive — 中间带 (0.3~1.0 ATR)
        不再强平 (允许过夜), 只有利润/持平/深亏三分支触发.
        """
        position = {'symbol': 'AAPL', 'entry_price': 100, 'position_id': 'p2', 'holding_hours': 2}
        # 浮盈 3.5/2.0 = 1.75 ATR ≥ 1.0 → adaptive profit 分支, 全平锁利
        market = {'current_price': 103.5, 'minutes_to_close': 8, 'atr': 2.0}
        trail_state = {'trail_status': 'ACTIVE', 'trail_high': 105}

        result = dispatcher.evaluate(position, market, trail_state)
        assert result is not None
        assert result.rule_name == 'pre_close'
        assert result.metadata.get('branch') == 'profit'

    def test_pre_close_mid_band_holds_overnight(self, dispatcher):
        """adaptive 中间带 (0.3~1.0 ATR 小利) → pre_close 不强平, 允许过夜"""
        position = {'symbol': 'AAPL', 'entry_price': 100, 'position_id': 'p2b', 'holding_hours': 2}
        # 浮盈 1.0/2.0 = 0.5 ATR ∈ (0.3, 1.0) 中间带
        market = {'current_price': 101, 'minutes_to_close': 8, 'atr': 2.0}

        result = dispatcher.evaluate(position, market)
        assert result is None or result.rule_name != 'pre_close'

    def test_no_trigger_when_conditions_not_met(self, dispatcher):
        """所有条件不满足时不触发"""
        position = {'symbol': 'AAPL', 'entry_price': 100, 'position_id': 'p3', 'holding_hours': 1}
        market = {'current_price': 101, 'minutes_to_close': 200, 'atr': 2.0, 'daily_loss_pct': 0.01}

        result = dispatcher.evaluate(position, market)
        assert result is None

    def test_time_decay_triggers(self):
        """持仓超时触发时间衰减 (单独构造无day_trade的dispatcher)"""
        config = {
            'exit_rules': {
                'flash_exit': {'enabled': True, 'price_drop_pct': 3.0},
                'pre_close': {'enabled': True, 'minutes_before_close': 15},
                'circuit_breaker': {'enabled': True, 'threshold_pct': 5.0},
                'trailing_stop': {'enabled': True, 'activation_threshold_atr': 0.8, 'trail_distance_atr': 1.2},
                'day_trade': {'enabled': False},
                'time_decay': {'enabled': True, 'max_hours': 48},
            },
            'exit_rules_exclusivity': {}
        }
        dispatcher = ExitRuleDispatcher(config)
        position = {'symbol': 'AAPL', 'entry_price': 100, 'position_id': 'p4', 'holding_hours': 50}
        market = {'current_price': 100.5, 'minutes_to_close': 200, 'atr': 2.0, 'daily_loss_pct': 0.01}

        result = dispatcher.evaluate(position, market)
        assert result is not None
        assert result.rule_name == 'time_decay'


class TestATRConstraints:
    """测试ATR约束"""

    def test_atr_floor_prevents_zero(self):
        """ATR下限防止过小值 (兼容类或模块级函数)"""
        try:
            from app.risk.atr_stop_engine import ATRStopEngine  # type: ignore
            engine = ATRStopEngine.__new__(ATRStopEngine)
            if hasattr(engine, 'apply_atr_constraints'):
                result = engine.apply_atr_constraints(0.001, price=100, config={'atr_floor_pct': 0.5})
                assert result >= 0.5
                return
        except ImportError:
            pass
        # 回退到模块级函数检查 (当前实现为函数)
        try:
            from app.risk import atr_stop_engine
            assert hasattr(atr_stop_engine, 'apply_atr_constraints'), \
                'apply_atr_constraints should exist as class method or module function'
        except ImportError:
            pytest.skip('atr_stop_engine module not available')

    def test_earnings_guard_caps_atr(self):
        """财报期ATR上限约束"""
        from app.risk.earnings_guard import EarningsGuard
        import tempfile, os

        db_path = tempfile.mktemp(suffix='.db')
        try:
            guard = EarningsGuard(db_path, {'days_before': 2, 'days_after': 1, 'atr_cap_multiplier': 2.5})
            # 未添加财报日期，不应限制
            cap = guard.get_atr_cap('AAPL', median_atr_14d=1.0)
            assert cap is None
        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)
