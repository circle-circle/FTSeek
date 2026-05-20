import pytest
from app.risk.drawdown_control import DrawdownController

def test_daily_drawdown_limit():
    """测试日内回撤限制"""
    controller = DrawdownController(
        max_daily_drawdown=0.03,  # 3%
        max_total_drawdown=0.08,  # 8%
    )
    
    # 初始状态
    assert controller.is_trading_allowed() == True
    
    # 模拟日内回撤 2%
    controller.update_drawdown(daily_dd=0.02, total_dd=0.05)
    assert controller.is_trading_allowed() == True
    
    # 模拟日内回撤 4% (超限)
    controller.update_drawdown(daily_dd=0.04, total_dd=0.05)
    assert controller.is_trading_allowed() == False
    assert controller.get_block_reason() == "DAILY_DRAWDOWN"

def test_total_drawdown_limit():
    """测试总回撤限制"""
    controller = DrawdownController(
        max_daily_drawdown=0.03,
        max_total_drawdown=0.08,
    )
    
    # 日内回撤正常，但总回撤超限
    controller.update_drawdown(daily_dd=0.02, total_dd=0.09)
    assert controller.is_trading_allowed() == False
    assert controller.get_block_reason() == "TOTAL_DRAWDOWN"

def test_drawdown_reset():
    """测试回撤控制器重置"""
    controller = DrawdownController(
        max_daily_drawdown=0.03,
        max_total_drawdown=0.08,
    )
    
    # 触发回撤限制
    controller.update_drawdown(daily_dd=0.04, total_dd=0.09)
    assert controller.is_trading_allowed() == False
    
    # 重置后应该允许交易
    controller.reset()
    assert controller.is_trading_allowed() == True
