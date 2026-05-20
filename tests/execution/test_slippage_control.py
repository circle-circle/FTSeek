# tests/execution/test_slippage_control.py
import pytest
from app.execution.slippage_control import SlippageController

def test_slippage_within_tolerance():
    """测试滑点在容忍范围内"""
    controller = SlippageController(max_slippage_ratio=0.005)  # 0.5%
    
    expected_price = 100.0
    actual_price = 100.3  # 0.3% 滑点
    
    assert controller.is_acceptable(expected_price, actual_price, "BUY") == True

def test_slippage_exceeds_tolerance():
    """测试滑点超出容忍范围"""
    controller = SlippageController(max_slippage_ratio=0.005)
    
    expected_price = 100.0
    actual_price = 101.0  # 1% 滑点
    
    assert controller.is_acceptable(expected_price, actual_price, "BUY") == False
