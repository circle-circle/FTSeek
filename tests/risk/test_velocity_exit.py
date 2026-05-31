"""Velocity Exit 单元测试

Phase 3 (2026-05-31): 验证速度捕捉退出规则的各种场景
"""
import pytest
import time
from app.risk.exit_rules.velocity_exit import check_velocity_exit


def test_velocity_spike_triggers():
    """价格急涨 (LONG) → 触发"""
    position = {"direction": "LONG", "entry_price": 100.0, "symbol": "TEST"}
    # Prices: gradual then spike
    prices = [100, 100.1, 100.2, 100.1, 100.3, 100.2, 100.1, 100.5, 100.4, 103.0]
    market_data = {"current_price": 103.0, "price_history": prices, "atr": 2.0}
    result = check_velocity_exit(
        position, market_data, {},
        {"enabled": True, "sigma_multiplier": 2.0},
    )
    assert result is not None
    assert result["reason"] == "VELOCITY_EXIT"
    assert result["close_pct"] == 30
    assert result["close_ratio"] == pytest.approx(0.30)
    assert "velocity_ratio" in result["metadata"]
    assert result["metadata"]["velocity_ratio"] > 2.0


def test_no_spike_no_trigger():
    """平稳价格 → 不触发"""
    position = {"direction": "LONG", "entry_price": 100.0, "symbol": "TEST"}
    prices = [100, 100.1, 100.0, 100.1, 100.0, 100.1, 100.0, 100.1, 100.0, 100.1]
    market_data = {"current_price": 100.1, "price_history": prices, "atr": 2.0}
    result = check_velocity_exit(position, market_data, {}, {"enabled": True})
    assert result is None


def test_adverse_spike_no_trigger():
    """逆向急跌 (LONG 持仓但价格急跌) → 不触发"""
    position = {"direction": "LONG", "entry_price": 100.0, "symbol": "TEST"}
    prices = [100, 100.1, 100.2, 100.1, 100.3, 100.2, 100.1, 99.5, 98.0, 97.0]
    market_data = {"current_price": 97.0, "price_history": prices, "atr": 2.0}
    result = check_velocity_exit(position, market_data, {}, {"enabled": True})
    assert result is None


def test_short_favorable_spike():
    """SHORT 持仓 + 价格急跌 → 触发"""
    position = {"direction": "SHORT", "entry_price": 100.0, "symbol": "TEST"}
    prices = [100, 99.9, 100.0, 99.8, 100.1, 99.9, 100.0, 99.5, 98.0, 97.0]
    market_data = {"current_price": 97.0, "price_history": prices, "atr": 2.0}
    result = check_velocity_exit(
        position, market_data, {},
        {"enabled": True, "sigma_multiplier": 2.0},
    )
    assert result is not None
    assert result["reason"] == "VELOCITY_EXIT"


def test_short_adverse_no_trigger():
    """SHORT 持仓 + 价格急涨 (逆向) → 不触发"""
    position = {"direction": "SHORT", "entry_price": 100.0, "symbol": "TEST"}
    prices = [100, 100.1, 100.2, 100.1, 100.3, 100.2, 100.1, 100.5, 100.4, 103.0]
    market_data = {"current_price": 103.0, "price_history": prices, "atr": 2.0}
    result = check_velocity_exit(position, market_data, {}, {"enabled": True})
    assert result is None


def test_cooldown_prevents_retrigger():
    """冷却期内不重复触发"""
    position = {"direction": "LONG", "entry_price": 100.0, "symbol": "TEST"}
    prices = [100, 100.1, 100.2, 100.1, 100.3, 100.2, 100.1, 100.5, 100.4, 103.0]
    market_data = {"current_price": 103.0, "price_history": prices, "atr": 2.0}
    trail_state = {}
    # First trigger
    result1 = check_velocity_exit(
        position, market_data, trail_state,
        {"enabled": True, "sigma_multiplier": 2.0},
    )
    assert result1 is not None
    # Second call within cooldown
    result2 = check_velocity_exit(
        position, market_data, trail_state,
        {"enabled": True, "sigma_multiplier": 2.0, "cooldown_seconds": 60},
    )
    assert result2 is None


def test_cooldown_expired_retrigger():
    """冷却期过后可以重新触发"""
    position = {"direction": "LONG", "entry_price": 100.0, "symbol": "TEST"}
    prices = [100, 100.1, 100.2, 100.1, 100.3, 100.2, 100.1, 100.5, 100.4, 103.0]
    market_data = {"current_price": 103.0, "price_history": prices, "atr": 2.0}
    trail_state = {}
    cfg = {"enabled": True, "sigma_multiplier": 2.0, "cooldown_seconds": 0}
    result1 = check_velocity_exit(position, market_data, trail_state, cfg)
    assert result1 is not None
    # Cooldown is 0, should retrigger immediately
    result2 = check_velocity_exit(position, market_data, trail_state, cfg)
    assert result2 is not None


def test_disabled_config():
    """enabled=False → 不触发"""
    position = {"direction": "LONG", "entry_price": 100.0, "symbol": "TEST"}
    prices = [100, 100.1, 100.2, 100.1, 100.3, 100.2, 100.1, 100.5, 100.4, 103.0]
    market_data = {"current_price": 103.0, "price_history": prices, "atr": 2.0}
    result = check_velocity_exit(position, market_data, {}, {"enabled": False})
    assert result is None


def test_no_price_history():
    """无价格历史 → 不触发"""
    position = {"direction": "LONG", "entry_price": 100.0, "symbol": "TEST"}
    market_data = {"current_price": 103.0, "atr": 2.0}
    result = check_velocity_exit(position, market_data, {}, {"enabled": True})
    assert result is None


def test_insufficient_price_history():
    """价格历史不足 (<5) → 不触发"""
    position = {"direction": "LONG", "entry_price": 100.0, "symbol": "TEST"}
    market_data = {"current_price": 103.0, "price_history": [100, 101, 102], "atr": 2.0}
    result = check_velocity_exit(position, market_data, {}, {"enabled": True})
    assert result is None


def test_zero_entry_price():
    """入场价为 0 → 不触发"""
    position = {"direction": "LONG", "entry_price": 0.0, "symbol": "TEST"}
    prices = [100, 100.1, 100.2, 100.1, 100.3, 100.2, 100.1, 100.5, 100.4, 103.0]
    market_data = {"current_price": 103.0, "price_history": prices, "atr": 2.0}
    result = check_velocity_exit(position, market_data, {}, {"enabled": True})
    assert result is None


def test_kline_data_fallback():
    """price_history 为空但 klines 可用 → 从 klines 提取"""
    position = {"direction": "LONG", "entry_price": 100.0, "symbol": "TEST"}
    klines = [
        {"close": 100}, {"close": 100.1}, {"close": 100.2}, {"close": 100.1},
        {"close": 100.3}, {"close": 100.2}, {"close": 100.1}, {"close": 100.5},
        {"close": 100.4}, {"close": 103.0},
    ]
    market_data = {"current_price": 103.0, "klines": klines, "atr": 2.0}
    result = check_velocity_exit(
        position, market_data, {},
        {"enabled": True, "sigma_multiplier": 2.0},
    )
    assert result is not None


def test_trail_state_updated():
    """触发后 trail_state 被正确更新"""
    position = {"direction": "LONG", "entry_price": 100.0, "symbol": "TEST"}
    prices = [100, 100.1, 100.2, 100.1, 100.3, 100.2, 100.1, 100.5, 100.4, 103.0]
    market_data = {"current_price": 103.0, "price_history": prices, "atr": 2.0}
    trail_state = {}
    result = check_velocity_exit(
        position, market_data, trail_state,
        {"enabled": True, "sigma_multiplier": 2.0, "trail_mult": 1.0},
    )
    assert result is not None
    assert "_last_velocity_exit_ts" in trail_state
    assert trail_state["velocity_trail_active"] is True
    assert trail_state["velocity_trail_mult"] == pytest.approx(1.0)
    assert "velocity_trail_expire_ts" in trail_state


def test_custom_close_pct():
    """自定义 close_pct 配置"""
    position = {"direction": "LONG", "entry_price": 100.0, "symbol": "TEST"}
    prices = [100, 100.1, 100.2, 100.1, 100.3, 100.2, 100.1, 100.5, 100.4, 103.0]
    market_data = {"current_price": 103.0, "price_history": prices, "atr": 2.0}
    result = check_velocity_exit(
        position, market_data, {},
        {"enabled": True, "sigma_multiplier": 2.0, "close_pct": 50},
    )
    assert result is not None
    assert result["close_pct"] == 50
    assert result["close_ratio"] == pytest.approx(0.50)


def test_position_object_interface():
    """支持 position 对象 (非 dict) 接口"""
    class MockPosition:
        direction = "LONG"
        avg_entry_price = 100.0
        symbol = "TEST"

    position = MockPosition()
    prices = [100, 100.1, 100.2, 100.1, 100.3, 100.2, 100.1, 100.5, 100.4, 103.0]
    market_data = {"current_price": 103.0, "price_history": prices, "atr": 2.0}
    result = check_velocity_exit(
        position, market_data, {},
        {"enabled": True, "sigma_multiplier": 2.0},
    )
    assert result is not None
    assert result["reason"] == "VELOCITY_EXIT"
