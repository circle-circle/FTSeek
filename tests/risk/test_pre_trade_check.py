# tests/risk/test_pre_trade_check.py
import pytest
from app.risk.pre_trade_check import PreTradeChecker
from app.core.models import SignalScore, Direction

def test_pre_trade_check_allows_good_signal():
    """测试预交易检查：好信号应该通过"""
    checker = PreTradeChecker()
    
    signal = SignalScore(
        symbol="AAPL",
        direction=Direction.LONG,
        strength=75,
        confidence=0.8,
    )
    
    result = checker.check(signal)
    assert result.allowed == True
    assert result.reason is None

def test_pre_trade_check_rejects_low_confidence():
    """测试预交易检查：低置信度应该拒绝"""
    checker = PreTradeChecker(min_confidence=0.60)
    
    signal = SignalScore(
        symbol="AAPL",
        direction=Direction.LONG,
        strength=50,
        confidence=0.40,  # 低于阈值
    )
    
    result = checker.check(signal)
    assert result.allowed == False
    assert "LOW_CONFIDENCE" in result.reason
