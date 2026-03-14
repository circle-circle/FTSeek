"""
单元测试：止损止盈检查逻辑

测试 PositionManager 的止损止盈功能：
- check_stop_loss()
- check_take_profit()
- calculate_stop_loss_price()
- calculate_take_profit_price()
"""

import pytest
import os
import tempfile
from app.trader.position_manager import PositionManager


@pytest.fixture
def position_manager():
    """创建临时数据库的 PositionManager 实例"""
    # 创建临时数据库文件
    temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
    temp_db.close()
    
    pm = PositionManager(db_path=temp_db.name)
    yield pm
    
    # 清理临时文件
    if os.path.exists(temp_db.name):
        os.unlink(temp_db.name)


@pytest.fixture
def setup_position(position_manager):
    """创建测试持仓"""
    # 创建测试持仓：买入价 100 元
    position_manager.create_position(
        code='000001',
        action='BUY',
        quantity=1000,
        entry_price=100.0,
        entry_time='2026-03-13 09:30:00'
    )
    return position_manager


class TestStopLoss:
    """测试止损功能"""
    
    def test_stop_loss_triggered(self, setup_position):
        """测试触发止损：价格下跌 5%"""
        pm = setup_position
        # 当前价 95 元，亏损 5%，应触发止损
        assert pm.check_stop_loss('000001', 95.0) is True
    
    def test_stop_loss_not_triggered(self, setup_position):
        """测试未触发止损：价格下跌 4%"""
        pm = setup_position
        # 当前价 96 元，亏损 4%，不触发止损
        assert pm.check_stop_loss('000001', 96.0) is False
    
    def test_stop_loss_profit(self, setup_position):
        """测试盈利时不触发止损"""
        pm = setup_position
        # 当前价 110 元，盈利 10%，不触发止损
        assert pm.check_stop_loss('000001', 110.0) is False
    
    def test_stop_loss_no_position(self, position_manager):
        """测试无持仓时不触发止损"""
        # 不存在该股票代码的持仓
        assert position_manager.check_stop_loss('000001', 90.0) is False
    
    def test_stop_loss_boundary(self, setup_position):
        """测试止损边界条件：正好 -5%"""
        pm = setup_position
        # 当前价 95 元，正好 -5%，应触发止损
        assert pm.check_stop_loss('000001', 95.0) is True
        
        # 当前价 95.01 元，略高于 -5%，不触发止损
        assert pm.check_stop_loss('000001', 95.01) is False
        
        # 当前价 94.99 元，略低于 -5%，触发止损
        assert pm.check_stop_loss('000001', 94.99) is True


class TestTakeProfit:
    """测试止盈功能"""
    
    def test_take_profit_triggered(self, setup_position):
        """测试触发止盈：价格上涨 15%"""
        pm = setup_position
        # 当前价 115 元，盈利 15%，应触发止盈
        assert pm.check_take_profit('000001', 115.0) is True
    
    def test_take_profit_not_triggered(self, setup_position):
        """测试未触发止盈：价格上涨 14%"""
        pm = setup_position
        # 当前价 114 元，盈利 14%，不触发止盈
        assert pm.check_take_profit('000001', 114.0) is False
    
    def test_take_profit_loss(self, setup_position):
        """测试亏损时不触发止盈"""
        pm = setup_position
        # 当前价 90 元，亏损 10%，不触发止盈
        assert pm.check_take_profit('000001', 90.0) is False
    
    def test_take_profit_no_position(self, position_manager):
        """测试无持仓时不触发止盈"""
        # 不存在该股票代码的持仓
        assert position_manager.check_take_profit('000001', 120.0) is False
    
    def test_take_profit_boundary(self, setup_position):
        """测试止盈边界条件：正好 +15%"""
        pm = setup_position
        # 当前价 115 元，正好 +15%，应触发止盈
        assert pm.check_take_profit('000001', 115.0) is True
        
        # 当前价 114.99 元，略低于 +15%，不触发止盈
        assert pm.check_take_profit('000001', 114.99) is False
        
        # 当前价 115.01 元，略高于 +15%，触发止盈
        assert pm.check_take_profit('000001', 115.01) is True


class TestStopLossPriceCalculation:
    """测试止损价格计算"""
    
    def test_calculate_stop_loss_price(self, position_manager):
        """测试止损价格计算：100 元成本"""
        result = position_manager.calculate_stop_loss_price(100.0)
        assert result == 95.0
    
    def test_calculate_stop_loss_price_different_cost(self, position_manager):
        """测试不同成本的止损价格"""
        # 成本 50 元
        result = position_manager.calculate_stop_loss_price(50.0)
        assert result == 47.5
        
        # 成本 200 元
        result = position_manager.calculate_stop_loss_price(200.0)
        assert result == 190.0
    
    def test_calculate_stop_loss_price_precision(self, position_manager):
        """测试止损价格精度"""
        # 成本 99.99 元
        result = position_manager.calculate_stop_loss_price(99.99)
        assert result == 94.99  # 保留 2 位小数


class TestTakeProfitPriceCalculation:
    """测试止盈价格计算"""
    
    def test_calculate_take_profit_price(self, position_manager):
        """测试止盈价格计算：100 元成本"""
        result = position_manager.calculate_take_profit_price(100.0)
        assert result == 115.0
    
    def test_calculate_take_profit_price_different_cost(self, position_manager):
        """测试不同成本的止盈价格"""
        # 成本 50 元
        result = position_manager.calculate_take_profit_price(50.0)
        assert result == 57.5
        
        # 成本 200 元
        result = position_manager.calculate_take_profit_price(200.0)
        assert result == 230.0
    
    def test_calculate_take_profit_price_precision(self, position_manager):
        """测试止盈价格精度"""
        # 成本 99.99 元
        result = position_manager.calculate_take_profit_price(99.99)
        assert result == 114.99  # 保留 2 位小数


class TestIntegration:
    """集成测试：止损止盈联合场景"""
    
    def test_stop_loss_before_take_profit(self, setup_position):
        """测试先触发止损的场景"""
        pm = setup_position
        # 价格下跌到止损线
        assert pm.check_stop_loss('000001', 95.0) is True
        assert pm.check_take_profit('000001', 95.0) is False
    
    def test_take_profit_before_stop_loss(self, setup_position):
        """测试先触发止盈的场景"""
        pm = setup_position
        # 价格上涨到止盈线
        assert pm.check_take_profit('000001', 115.0) is True
        assert pm.check_stop_loss('000001', 115.0) is False
    
    def test_neither_triggered(self, setup_position):
        """测试两者都未触发"""
        pm = setup_position
        # 价格在 -5% 和 +15% 之间
        assert pm.check_stop_loss('000001', 100.0) is False
        assert pm.check_take_profit('000001', 100.0) is False
        
        assert pm.check_stop_loss('000001', 105.0) is False
        assert pm.check_take_profit('000001', 105.0) is False
    
    def test_price_levels(self, setup_position):
        """测试不同价格水平的止损止盈状态"""
        pm = setup_position
        
        # 成本价 100 元
        # 止损价 95 元，止盈价 115 元
        
        # 严重亏损 (-10%)
        assert pm.check_stop_loss('000001', 90.0) is True
        assert pm.check_take_profit('000001', 90.0) is False
        
        # 小幅亏损 (-2%)
        assert pm.check_stop_loss('000001', 98.0) is False
        assert pm.check_take_profit('000001', 98.0) is False
        
        # 成本价 (0%)
        assert pm.check_stop_loss('000001', 100.0) is False
        assert pm.check_take_profit('000001', 100.0) is False
        
        # 小幅盈利 (+5%)
        assert pm.check_stop_loss('000001', 105.0) is False
        assert pm.check_take_profit('000001', 105.0) is False
        
        # 大幅盈利 (+20%)
        assert pm.check_stop_loss('000001', 120.0) is False
        assert pm.check_take_profit('000001', 120.0) is True
