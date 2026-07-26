# tests/risk/test_exit_config.py
import pytest
from app.risk.exit_config import ExitConfig, StrategyType

def test_exit_config_default_values():
    """测试退出配置默认值 (P0 强化版: 单一 stop_loss 拆分为按策略类型分层)"""
    config = ExitConfig()

    # P0 强化: 做多止损按策略类型分层, 卖空 +8%
    assert config.short_stop_loss == 0.08
    assert config.long_stop_loss_short_term == -0.05
    assert config.long_stop_loss_mid_term == -0.08
    assert config.long_stop_loss_long_term == -0.15
    assert config.take_profit_1 == 0.08  # +8%
    assert config.take_profit_2 == 0.15  # +15%
    assert config.take_profit_3 == 0.25  # +25%
    assert config.trailing_stop_atr_mult == 2.0
    assert config.time_stop_hours == 72

def test_exit_config_get_levels():
    """测试获取退出价位 (默认 MID_TERM LONG: 止损 -8%)"""
    config = ExitConfig()
    entry_price = 100.0

    levels = config.get_exit_levels(entry_price)

    assert abs(levels["stop_loss"] - 92.0) < 0.001  # 100 * (1 - 0.08), MID_TERM
    assert abs(levels["take_profit_1"] - 108.0) < 0.001
    assert abs(levels["take_profit_2"] - 115.0) < 0.001
    assert abs(levels["take_profit_3"] - 125.0) < 0.001

    # 短线更紧: -5%
    short_levels = config.get_exit_levels(entry_price, StrategyType.SHORT_TERM)
    assert abs(short_levels["stop_loss"] - 95.0) < 0.001

    # 卖空止损: +8%
    short_dir_levels = config.get_exit_levels(entry_price, StrategyType.SHORT, direction="SHORT")
    assert abs(short_dir_levels["stop_loss"] - 108.0) < 0.001
