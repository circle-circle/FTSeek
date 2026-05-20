# tests/risk/test_exit_config.py
import pytest
from app.risk.exit_config import ExitConfig

def test_exit_config_default_values():
    """测试退出配置默认值"""
    config = ExitConfig()
    
    assert config.stop_loss == -0.06  # -6%
    assert config.take_profit_1 == 0.08  # +8%
    assert config.take_profit_2 == 0.15  # +15%
    assert config.take_profit_3 == 0.25  # +25%
    assert config.trailing_stop_atr_mult == 2.0
    assert config.time_stop_hours == 72

def test_exit_config_get_levels():
    """测试获取退出价位"""
    config = ExitConfig()
    entry_price = 100.0
    
    levels = config.get_exit_levels(entry_price)
    
    assert abs(levels["stop_loss"] - 94.0) < 0.001  # 100 * (1 - 0.06)
    assert abs(levels["take_profit_1"] - 108.0) < 0.001
    assert abs(levels["take_profit_2"] - 115.0) < 0.001
    assert abs(levels["take_profit_3"] - 125.0) < 0.001
