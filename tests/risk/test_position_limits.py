# tests/risk/test_position_limits.py
import pytest
from app.risk.position_limits import PositionLimitsChecker

def test_single_position_limit():
    """测试单品种仓位限制"""
    checker = PositionLimitsChecker(
        max_position_per_symbol=0.25,  # 25%
        max_position_per_direction=0.50,  # 50%
        max_total_position=0.80,  # 80%
    )
    
    # 正常仓位
    assert checker.check_position("AAPL", 0.20, 0.20, 0.20) == (True, None)
    
    # 单品种超限
    result, reason = checker.check_position("AAPL", 0.30, 0.20, 0.20)
    assert result == False
    assert reason == "SINGLE_SYMBOL_LIMIT"
