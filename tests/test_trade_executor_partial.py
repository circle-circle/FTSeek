"""
部分平仓功能单元测试
测试 TradeExecutor 的 execute_partial_close 方法
"""
import pytest
import sqlite3
import os
import sys
from unittest.mock import Mock, patch, MagicMock
from dataclasses import dataclass
from datetime import datetime

# 添加项目路径
sys.path.insert(0, '/opt/ftseek/FTSeek')

from app.trader.executor import TradeExecutor, TradeResult
from app.trader.position_manager import PositionManager
from app.strategy.base import Signal, SignalAction


class TestTradeResultPartialClose:
    """测试 TradeResult 数据类支持部分平仓字段"""
    
    def test_trade_result_has_partial_close_fields(self):
        """TradeResult 应该包含 close_ratio 和 remaining_quantity 字段"""
        result = TradeResult(
            success=True,
            order_id="12345",
            close_ratio=0.5,
            remaining_quantity=100
        )
        assert result.success is True
        assert result.order_id == "12345"
        assert result.close_ratio == 0.5
        assert result.remaining_quantity == 100
    
    def test_trade_result_default_values(self):
        """TradeResult 的部分平仓字段应该有默认值 None"""
        result = TradeResult(success=True)
        assert result.close_ratio is None
        assert result.remaining_quantity is None


class TestPositionManagerPartialClose:
    """测试 PositionManager 的部分平仓功能"""
    
    @pytest.fixture
    def temp_db(self, tmp_path):
        """创建临时数据库"""
        db_path = tmp_path / "test_positions.db"
        yield str(db_path)
        # 清理
        if os.path.exists(str(db_path)):
            os.remove(str(db_path))
    
    @pytest.fixture
    def position_manager(self, temp_db):
        """创建 PositionManager 实例"""
        pm = PositionManager(db_path=temp_db)
        # 创建测试持仓
        pm.create_position(
            code="HK.00700",
            action="BUY",
            quantity=200,
            entry_price=300.0,
            entry_time="2024-01-01 10:00:00"
        )
        return pm
    
    def test_partial_close_position_success(self, position_manager):
        """测试部分平仓成功"""
        # 执行部分平仓 50%
        pnl = position_manager.partial_close_position(
            code="HK.00700",
            close_qty=100,
            exit_price=350.0,
            exit_time="2024-01-02 10:00:00"
        )
        
        # 验证盈亏计算正确 (350-300)*100 = 5000
        assert pnl == 5000.0
        
        # 验证持仓数量减少
        position = position_manager.get_position("HK.00700")
        assert position.quantity == 100
        assert position.status == 'OPEN'  # 状态保持 OPEN
    
    def test_partial_close_position_full_close(self, position_manager):
        """测试平仓数量等于持仓数量时全部平仓"""
        pnl = position_manager.partial_close_position(
            code="HK.00700",
            close_qty=200,
            exit_price=350.0,
            exit_time="2024-01-02 10:00:00"
        )
        
        # 验证盈亏计算正确
        assert pnl == 10000.0
        
        # 验证持仓已关闭
        position = position_manager.get_position("HK.00700")
        assert position is None  # 没有 OPEN 状态的持仓
    
    def test_partial_close_position_no_position(self, position_manager):
        """测试不存在的持仓"""
        pnl = position_manager.partial_close_position(
            code="HK.99999",
            close_qty=100,
            exit_price=350.0,
            exit_time="2024-01-02 10:00:00"
        )
        assert pnl == 0.0
    
    def test_partial_close_position_sell_action(self, temp_db):
        """测试做空持仓的部分平仓"""
        pm = PositionManager(db_path=temp_db)
        pm.create_position(
            code="HK.00700",
            action="SELL",
            quantity=200,
            entry_price=300.0,
            entry_time="2024-01-01 10:00:00"
        )
        
        # 价格下跌，做空盈利
        pnl = pm.partial_close_position(
            code="HK.00700",
            close_qty=100,
            exit_price=250.0,
            exit_time="2024-01-02 10:00:00"
        )
        
        # 验证盈亏计算正确 (300-250)*100 = 5000
        assert pnl == 5000.0
        
        # 验证持仓数量减少
        position = pm.get_position("HK.00700")
        assert position.quantity == 100


class TestTradeExecutorPartialClose:
    """测试 TradeExecutor 的部分平仓功能"""
    
    @pytest.fixture
    def temp_db(self, tmp_path):
        """创建临时数据库"""
        db_path = tmp_path / "test_executor.db"
        yield str(db_path)
        if os.path.exists(str(db_path)):
            os.remove(str(db_path))
    
    @pytest.fixture
    def executor(self, temp_db):
        """创建 TradeExecutor 实例 (使用临时数据库)"""
        with patch('app.trader.executor.PositionManager') as MockPM:
            mock_pm = Mock()
            mock_pm.get_position.return_value = Mock(
                id=1,
                code="HK.00700",
                action="BUY",
                quantity=200,
                entry_price=300.0
            )
            mock_pm.partial_close_position.return_value = 5000.0
            
            executor = TradeExecutor()
            executor.position_manager = mock_pm
            return executor
    
    def _create_mock_order_data(self, order_id='12345'):
        """创建模拟的订单数据 (模仿 pandas DataFrame)"""
        mock_data = Mock()
        mock_data.__len__ = Mock(return_value=1)
        mock_data.iloc = Mock()
        mock_data.iloc.__getitem__ = Mock(return_value={'order_id': order_id})
        return mock_data
    
    def test_execute_partial_close_success(self, executor):
        """测试部分平仓成功"""
        signal = Signal(
            code="HK.00700",
            action=SignalAction.SELL,
            price=350.0,
            quantity=100
        )
        
        with patch.object(executor, '_get_trd_ctx') as mock_get_ctx:
            # Mock 交易上下文
            mock_ctx = Mock()
            mock_ctx.place_order.return_value = (0, self._create_mock_order_data('12345'))
            mock_get_ctx.return_value = mock_ctx
            
            result = executor.execute_partial_close(signal, close_ratio=0.5)
            
            # 验证结果
            assert result.success is True
            assert result.order_id == "12345"
            assert result.trade_type == 'PARTIAL_CLOSE'
            assert result.close_ratio == 0.5
            assert result.remaining_quantity == 100
            assert result.pnl == 5000.0
    
    def test_execute_partial_close_invalid_ratio(self, executor):
        """测试无效的平仓比例"""
        signal = Signal(
            code="HK.00700",
            action=SignalAction.SELL,
            price=350.0,
            quantity=100
        )
        
        # 测试 ratio <= 0
        result = executor.execute_partial_close(signal, close_ratio=0)
        assert result.success is False
        assert "无效的平仓比例" in result.message
        
        # 测试 ratio > 1
        result = executor.execute_partial_close(signal, close_ratio=1.5)
        assert result.success is False
        assert "无效的平仓比例" in result.message
    
    def test_execute_partial_close_no_position(self, executor):
        """测试没有持仓时部分平仓失败"""
        executor.position_manager.get_position.return_value = None
        
        signal = Signal(
            code="HK.00700",
            action=SignalAction.SELL,
            price=350.0,
            quantity=100
        )
        
        result = executor.execute_partial_close(signal, close_ratio=0.5)
        
        assert result.success is False
        assert "未找到" in result.message
    
    def test_execute_partial_close_order_failed(self, executor):
        """测试下单失败"""
        signal = Signal(
            code="HK.00700",
            action=SignalAction.SELL,
            price=350.0,
            quantity=100
        )
        
        with patch.object(executor, '_get_trd_ctx') as mock_get_ctx:
            mock_ctx = Mock()
            mock_ctx.place_order.return_value = (-1, "Order failed")
            mock_get_ctx.return_value = mock_ctx
            
            result = executor.execute_partial_close(signal, close_ratio=0.5)
            
            assert result.success is False
            assert "下单失败" in result.message
    
    def test_execute_partial_close_minimum_qty(self, executor):
        """测试最小平仓数量 (至少 1 股)"""
        # 设置小数量持仓
        executor.position_manager.get_position.return_value = Mock(
            id=1,
            code="HK.00700",
            action="BUY",
            quantity=3,
            entry_price=300.0
        )
        
        signal = Signal(
            code="HK.00700",
            action=SignalAction.SELL,
            price=350.0,
            quantity=3
        )
        
        with patch.object(executor, '_get_trd_ctx') as mock_get_ctx:
            mock_ctx = Mock()
            mock_ctx.place_order.return_value = (0, self._create_mock_order_data('12345'))
            mock_get_ctx.return_value = mock_ctx
            
            # 平仓 10% (3*0.1=0.3，应该向上取整到 1)
            result = executor.execute_partial_close(signal, close_ratio=0.1)
            
            # 验证至少平仓 1 股
            assert result.success is True
            assert result.remaining_quantity == 2  # 3-1=2


class TestIntegrationPartialClose:
    """集成测试：部分平仓完整流程"""
    
    @pytest.fixture
    def temp_db(self, tmp_path):
        """创建临时数据库"""
        db_path = tmp_path / "test_integration.db"
        yield str(db_path)
        if os.path.exists(str(db_path)):
            os.remove(str(db_path))
    
    def test_partial_close_workflow(self, temp_db):
        """测试部分平仓完整工作流程"""
        # 1. 创建持仓管理器并建立持仓
        pm = PositionManager(db_path=temp_db)
        position_id = pm.create_position(
            code="HK.00700",
            action="BUY",
            quantity=200,
            entry_price=300.0,
            entry_time="2024-01-01 10:00:00"
        )
        
        # 2. 验证初始持仓
        position = pm.get_position("HK.00700")
        assert position.quantity == 200
        assert position.status == 'OPEN'
        
        # 3. 执行部分平仓 50%
        pnl1 = pm.partial_close_position(
            code="HK.00700",
            close_qty=100,
            exit_price=350.0,
            exit_time="2024-01-02 10:00:00"
        )
        
        # 4. 验证第一次部分平仓结果
        assert pnl1 == 5000.0  # (350-300)*100
        position = pm.get_position("HK.00700")
        assert position.quantity == 100
        assert position.status == 'OPEN'
        
        # 5. 再次部分平仓 50% (剩余的 50%)
        pnl2 = pm.partial_close_position(
            code="HK.00700",
            close_qty=50,
            exit_price=360.0,
            exit_time="2024-01-03 10:00:00"
        )
        
        # 6. 验证第二次部分平仓结果
        assert pnl2 == 3000.0  # (360-300)*50
        position = pm.get_position("HK.00700")
        assert position.quantity == 50
        assert position.status == 'OPEN'
        
        # 7. 全部平仓
        pnl3 = pm.partial_close_position(
            code="HK.00700",
            close_qty=50,
            exit_price=370.0,
            exit_time="2024-01-04 10:00:00"
        )
        
        # 8. 验证全部平仓结果
        assert pnl3 == 3500.0  # (370-300)*50
        position = pm.get_position("HK.00700")
        assert position is None  # 没有 OPEN 状态的持仓
        
        # 9. 验证总盈亏
        total_pnl = pnl1 + pnl2 + pnl3
        assert total_pnl == 11500.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
