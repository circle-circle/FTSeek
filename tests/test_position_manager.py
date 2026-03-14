"""
持仓管理器单元测试
"""

import pytest
import os
import tempfile
import sqlite3
from datetime import datetime

from app.trader.position_manager import PositionManager, Position


class TestPositionManager:
    """PositionManager 测试类"""
    
    @pytest.fixture
    def temp_db(self):
        """创建临时数据库用于测试"""
        fd, path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        yield path
        # 清理临时文件
        if os.path.exists(path):
            os.remove(path)
    
    @pytest.fixture
    def position_manager(self, temp_db):
        """创建 PositionManager 实例"""
        return PositionManager(db_path=temp_db)
    
    def test_create_position(self, position_manager):
        """测试创建持仓"""
        # 创建持仓
        position_id = position_manager.create_position(
            code='000001',
            action='BUY',
            quantity=100,
            entry_price=10.5,
            entry_time='2024-01-15 09:30:00'
        )
        
        # 验证返回的 ID
        assert position_id is not None
        assert position_id > 0
        
        # 验证持仓已创建
        position = position_manager.get_position('000001')
        assert position is not None
        assert position.code == '000001'
        assert position.action == 'BUY'
        assert position.quantity == 100
        assert position.entry_price == 10.5
        assert position.entry_time == '2024-01-15 09:30:00'
        assert position.status == 'OPEN'
    
    def test_get_position(self, position_manager):
        """测试获取持仓"""
        # 先创建持仓
        position_manager.create_position(
            code='000002',
            action='BUY',
            quantity=200,
            entry_price=20.0,
            entry_time='2024-01-16 10:00:00'
        )
        
        # 获取持仓
        position = position_manager.get_position('000002')
        
        assert position is not None
        assert position.code == '000002'
        assert position.quantity == 200
        assert position.entry_price == 20.0
        
        # 测试获取不存在的持仓
        non_existent = position_manager.get_position('999999')
        assert non_existent is None
    
    def test_get_all_positions(self, position_manager):
        """测试获取所有持仓"""
        # 创建多个持仓
        position_manager.create_position(
            code='000001',
            action='BUY',
            quantity=100,
            entry_price=10.5,
            entry_time='2024-01-15 09:30:00'
        )
        position_manager.create_position(
            code='000002',
            action='SELL',
            quantity=200,
            entry_price=20.0,
            entry_time='2024-01-16 10:00:00'
        )
        
        # 获取所有持仓
        positions = position_manager.get_all_positions()
        
        assert len(positions) == 2
        codes = [p.code for p in positions]
        assert '000001' in codes
        assert '000002' in codes
    
    def test_close_position(self, position_manager):
        """测试平仓"""
        # 创建持仓
        position_manager.create_position(
            code='000003',
            action='BUY',
            quantity=100,
            entry_price=10.0,
            entry_time='2024-01-17 09:30:00'
        )
        
        # 平仓
        realized_pnl = position_manager.close_position(
            code='000003',
            exit_price=12.0,
            exit_time='2024-01-18 15:00:00'
        )
        
        # 验证已实现盈亏 (买入：(12.0 - 10.0) * 100 = 200)
        assert realized_pnl == 200.0
        
        # 验证持仓状态已更新
        position = position_manager.get_position('000003')
        assert position is None  # 因为 status 已变为 CLOSED
        
        # 直接查询数据库验证
        conn = sqlite3.connect(position_manager.db_path)
        cursor = conn.cursor()
        cursor.execute('SELECT status, exit_price, realized_pnl FROM positions WHERE code = ?', ('000003',))
        row = cursor.fetchone()
        conn.close()
        
        assert row[0] == 'CLOSED'
        assert row[1] == 12.0
        assert row[2] == 200.0
    
    def test_calculate_pnl_buy(self, position_manager):
        """测试买入盈亏计算"""
        # 买入场景：价格上涨盈利
        pnl = position_manager.calculate_pnl(
            entry_price=10.0,
            exit_price=12.0,
            quantity=100,
            action='BUY'
        )
        assert pnl == 200.0
        
        # 买入场景：价格下跌亏损
        pnl = position_manager.calculate_pnl(
            entry_price=10.0,
            exit_price=8.0,
            quantity=100,
            action='BUY'
        )
        assert pnl == -200.0
        
        # 买入场景：价格不变
        pnl = position_manager.calculate_pnl(
            entry_price=10.0,
            exit_price=10.0,
            quantity=100,
            action='BUY'
        )
        assert pnl == 0.0
    
    def test_calculate_pnl_sell(self, position_manager):
        """测试卖出 (做空) 盈亏计算"""
        # 卖出场景：价格下跌盈利
        pnl = position_manager.calculate_pnl(
            entry_price=10.0,
            exit_price=8.0,
            quantity=100,
            action='SELL'
        )
        assert pnl == 200.0
        
        # 卖出场景：价格上涨亏损
        pnl = position_manager.calculate_pnl(
            entry_price=10.0,
            exit_price=12.0,
            quantity=100,
            action='SELL'
        )
        assert pnl == -200.0
        
        # 卖出场景：价格不变
        pnl = position_manager.calculate_pnl(
            entry_price=10.0,
            exit_price=10.0,
            quantity=100,
            action='SELL'
        )
        assert pnl == 0.0
    
    def test_update_price(self, position_manager):
        """测试更新当前价和浮动盈亏"""
        # 创建持仓
        position_manager.create_position(
            code='000004',
            action='BUY',
            quantity=100,
            entry_price=10.0,
            entry_time='2024-01-19 09:30:00'
        )
        
        # 更新价格
        position_manager.update_price('000004', 11.5)
        
        # 验证更新
        position = position_manager.get_position('000004')
        assert position.current_price == 11.5
        assert position.unrealized_pnl == 150.0  # (11.5 - 10.0) * 100
    
    def test_update_price_sell_position(self, position_manager):
        """测试更新卖出 (做空) 持仓的价格"""
        # 创建卖出持仓
        position_manager.create_position(
            code='000005',
            action='SELL',
            quantity=100,
            entry_price=10.0,
            entry_time='2024-01-20 09:30:00'
        )
        
        # 更新价格 (价格下跌，做空盈利)
        position_manager.update_price('000005', 8.5)
        
        # 验证更新
        position = position_manager.get_position('000005')
        assert position.current_price == 8.5
        assert position.unrealized_pnl == 150.0  # (10.0 - 8.5) * 100
    
    def test_close_position_sell(self, position_manager):
        """测试平仓卖出 (做空) 持仓"""
        # 创建卖出持仓
        position_manager.create_position(
            code='000006',
            action='SELL',
            quantity=100,
            entry_price=10.0,
            entry_time='2024-01-21 09:30:00'
        )
        
        # 平仓 (价格下跌，盈利)
        realized_pnl = position_manager.close_position(
            code='000006',
            exit_price=8.0,
            exit_time='2024-01-22 15:00:00'
        )
        
        # 验证已实现盈亏 (卖出：(10.0 - 8.0) * 100 = 200)
        assert realized_pnl == 200.0
    
    def test_close_non_existent_position(self, position_manager):
        """测试平仓不存在的持仓"""
        # 平仓不存在的持仓应返回 0
        realized_pnl = position_manager.close_position(
            code='999999',
            exit_price=10.0,
            exit_time='2024-01-23 15:00:00'
        )
        assert realized_pnl == 0.0
    
    def test_position_dataclass_defaults(self, position_manager):
        """测试 Position 数据类的默认值"""
        position = Position(
            id=1,
            code='000001',
            action='BUY',
            quantity=100,
            entry_price=10.0,
            entry_time='2024-01-24 09:30:00'
        )
        
        assert position.current_price is None
        assert position.unrealized_pnl is None
        assert position.status == 'OPEN'
        assert position.exit_price is None
        assert position.realized_pnl is None


class TestPositionManagerIntegration:
    """PositionManager 集成测试"""
    
    @pytest.fixture
    def temp_db(self):
        """创建临时数据库用于测试"""
        fd, path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        yield path
        if os.path.exists(path):
            os.remove(path)
    
    def test_full_trading_lifecycle(self, temp_db):
        """测试完整的交易生命周期"""
        pm = PositionManager(db_path=temp_db)
        
        # 1. 开仓
        position_id = pm.create_position(
            code='000001',
            action='BUY',
            quantity=1000,
            entry_price=50.0,
            entry_time='2024-02-01 09:30:00'
        )
        assert position_id > 0
        
        # 2. 更新价格 (价格上涨)
        pm.update_price('000001', 52.0)
        position = pm.get_position('000001')
        assert position.unrealized_pnl == 2000.0  # (52.0 - 50.0) * 1000
        
        # 3. 继续更新价格
        pm.update_price('000001', 55.0)
        position = pm.get_position('000001')
        assert position.unrealized_pnl == 5000.0  # (55.0 - 50.0) * 1000
        
        # 4. 平仓
        realized_pnl = pm.close_position(
            code='000001',
            exit_price=55.0,
            exit_time='2024-02-05 15:00:00'
        )
        assert realized_pnl == 5000.0
        
        # 5. 验证持仓已关闭
        position = pm.get_position('000001')
        assert position is None
        
        # 6. 验证所有持仓为空
        all_positions = pm.get_all_positions()
        assert len(all_positions) == 0
    
    def test_multiple_positions(self, temp_db):
        """测试多个持仓同时管理"""
        pm = PositionManager(db_path=temp_db)
        
        # 创建多个持仓
        pm.create_position('000001', 'BUY', 100, 10.0, '2024-03-01 09:30:00')
        pm.create_position('000002', 'BUY', 200, 20.0, '2024-03-01 09:35:00')
        pm.create_position('000003', 'SELL', 150, 30.0, '2024-03-01 09:40:00')
        
        # 验证所有持仓
        positions = pm.get_all_positions()
        assert len(positions) == 3
        
        # 更新部分持仓价格
        pm.update_price('000001', 11.0)  # 盈利 100
        pm.update_price('000002', 19.0)  # 亏损 200
        pm.update_price('000003', 28.0)  # 盈利 300 (做空)
        
        # 验证更新
        pos1 = pm.get_position('000001')
        assert pos1.unrealized_pnl == 100.0
        
        pos2 = pm.get_position('000002')
        assert pos2.unrealized_pnl == -200.0
        
        pos3 = pm.get_position('000003')
        assert pos3.unrealized_pnl == 300.0
        
        # 平掉一个持仓
        pm.close_position('000001', 11.0, '2024-03-02 15:00:00')
        
        # 验证剩余持仓
        positions = pm.get_all_positions()
        assert len(positions) == 2


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
