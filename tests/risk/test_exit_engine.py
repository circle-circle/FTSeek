"""退出引擎测试"""
import pytest
from datetime import datetime, timedelta
from app.risk.exit_engine import ExitEngine, ExitReason
from app.core.models import Position, Direction


def test_exit_engine_triggers_stop_loss():
    """测试退出引擎：触发止损"""
    engine = ExitEngine()
    
    position = Position(
        symbol="AAPL",
        direction=Direction.LONG,
        avg_entry_price=100.0,
        size=100,
        initial_sl=94.0,  # -6%
    )
    
    # 当前价格跌破止损
    should_exit, reason = engine.should_exit(position, current_price=93.0)
    
    assert should_exit == True
    assert reason == ExitReason.STOP_LOSS


def test_exit_engine_triggers_take_profit():
    """测试退出引擎：触发止盈"""
    engine = ExitEngine()
    
    position = Position(
        symbol="AAPL",
        direction=Direction.LONG,
        avg_entry_price=100.0,
        size=100,
        initial_tp=108.0,  # +8%
    )
    
    # 当前价格突破止盈
    should_exit, reason = engine.should_exit(position, current_price=110.0)
    
    assert should_exit == True
    assert reason == ExitReason.TAKE_PROFIT


def test_exit_engine_no_exit():
    """测试退出引擎：不触发退出"""
    engine = ExitEngine()
    
    position = Position(
        symbol="AAPL",
        direction=Direction.LONG,
        avg_entry_price=100.0,
        size=100,
        initial_sl=94.0,
        initial_tp=108.0,
    )
    
    # 当前价格在止损和止盈之间
    should_exit, reason = engine.should_exit(position, current_price=100.0)
    
    assert should_exit == False
    assert reason is None


def test_exit_engine_short_stop_loss():
    """测试退出引擎：空头止损"""
    engine = ExitEngine()
    
    position = Position(
        symbol="AAPL",
        direction=Direction.SHORT,
        avg_entry_price=100.0,
        size=100,
        initial_sl=106.0,  # 空头止损在上方
    )
    
    # 当前价格上涨突破止损
    should_exit, reason = engine.should_exit(position, current_price=107.0)
    
    assert should_exit == True
    assert reason == ExitReason.STOP_LOSS


def test_exit_engine_short_take_profit():
    """测试退出引擎：空头止盈"""
    engine = ExitEngine()
    
    position = Position(
        symbol="AAPL",
        direction=Direction.SHORT,
        avg_entry_price=100.0,
        size=100,
        initial_sl=106.0,  # 空头止损在上方
        initial_tp=92.0,  # 空头止盈在下方
    )
    
    # 当前价格下跌突破止盈
    should_exit, reason = engine.should_exit(position, current_price=90.0)
    
    assert should_exit == True
    assert reason == ExitReason.TAKE_PROFIT
