"""
交易执行器 V2 单元测试

测试 TradeExecutor 的开仓和平仓功能
"""
import unittest
from unittest.mock import Mock, patch, MagicMock
import sys
import os
import sqlite3

# 添加项目路径
sys.path.insert(0, '/opt/ftseek/FTSeek')

from app.trader.executor import TradeExecutor, TradeResult
from app.trader.position_manager import PositionManager
from app.strategy.base import Signal, SignalAction


class MockRiskResult:
    """模拟风控结果"""
    def __init__(self, passed=True):
        self.passed = passed
        self.details = {'confidence': passed, 'position_limit': passed, 'amount_limit': passed}


class MockRiskManager:
    """模拟风控管理器"""
    def check_before_trade(self, signal):
        return MockRiskResult(passed=True)


class TestTradeExecutorV2(unittest.TestCase):
    """交易执行器 V2 测试"""
    
    def setUp(self):
        """测试前准备"""
        # 使用测试数据库
        self.test_db_path = "/tmp/ftseek_test_positions.db"
        
        # 清理旧测试数据
        if os.path.exists(self.test_db_path):
            os.remove(self.test_db_path)
        
        # 创建测试信号
        self.test_signal_buy = Signal(
            id="test_signal_001",
            code="US.AAPL",
            name="Apple Inc",
            action=SignalAction.BUY,
            price=175.50,
            quantity=100,
            reason="技术面突破",
            strategy="测试策略"
        )
        
        self.test_signal_sell = Signal(
            id="test_signal_002",
            code="US.AAPL",
            name="Apple Inc",
            action=SignalAction.SELL,
            price=180.00,
            quantity=100,
            reason="获利了结",
            strategy="测试策略"
        )
        
        self.test_signal_alert = Signal(
            id="test_signal_003",
            code="US.AAPL",
            name="Apple Inc",
            action=SignalAction.ALERT,
            price=175.50,
            quantity=100,
            reason="仅提醒",
            strategy="测试策略"
        )
    
    def tearDown(self):
        """测试后清理"""
        if os.path.exists(self.test_db_path):
            os.remove(self.test_db_path)
    
    @patch('app.trader.executor.config')
    @patch('app.trader.executor.get_risk_manager')
    @patch('app.trader.executor.PositionManager')
    def test_execute_open_buy(self, mock_pm_class, mock_risk, mock_config):
        """测试开仓买入"""
        # 配置模拟
        mock_config.OPEND_HOST = '127.0.0.1'
        mock_config.OPEND_PORT = 11111
        mock_config.TRADE_ENV = 1
        mock_config.DEFAULT_ORDER_TYPE = 'MARKET'
        mock_config.DEFAULT_ORDER_QTY_US = 100
        mock_config.MAX_ORDER_AMOUNT = 1000000
        
        # 模拟风控管理器
        mock_risk.return_value = MockRiskManager()
        
        # 模拟 PositionManager
        mock_pm = Mock()
        mock_pm.create_position.return_value = 123
        mock_pm_class.return_value = mock_pm
        
        # 模拟富途 API
        with patch('futu.OpenSecTradeContext') as mock_trd_ctx:
            mock_ret_data = MagicMock()
            mock_ret_data.iloc = [MagicMock()]
            mock_ret_data.iloc[0].__getitem__ = Mock(return_value='FUTU_ORDER_001')
            type(mock_ret_data).__len__ = Mock(return_value=1)
            
            mock_trd_ctx.return_value.place_order.return_value = (0, mock_ret_data)
            
            # 创建执行器（使用测试数据库）
            executor = TradeExecutor()
            executor.position_manager = PositionManager(self.test_db_path)
            
            # 执行开仓
            result = executor.execute(self.test_signal_buy, trade_type='OPEN')
            
            # 验证结果
            self.assertIsInstance(result, TradeResult)
            self.assertTrue(result.success)
            self.assertEqual(result.trade_type, 'OPEN')
            self.assertIsNotNone(result.position_id)
            self.assertIsNotNone(result.order_id)
            
            # 验证持仓已创建
            position = executor.position_manager.get_position('US.AAPL')
            self.assertIsNotNone(position)
            self.assertEqual(position.code, 'US.AAPL')
            self.assertEqual(position.action, 'BUY')
            self.assertEqual(position.quantity, 100)
            self.assertEqual(position.entry_price, 175.50)
            self.assertEqual(position.status, 'OPEN')
    
    @patch('app.trader.executor.config')
    @patch('app.trader.executor.get_risk_manager')
    def test_execute_open_sell(self, mock_risk, mock_config):
        """测试开仓卖出（做空）"""
        # 配置模拟
        mock_config.OPEND_HOST = '127.0.0.1'
        mock_config.OPEND_PORT = 11111
        mock_config.TRADE_ENV = 1
        mock_config.DEFAULT_ORDER_TYPE = 'MARKET'
        mock_config.DEFAULT_ORDER_QTY_US = 100
        mock_config.MAX_ORDER_AMOUNT = 1000000
        
        # 模拟风控管理器
        mock_risk.return_value = MockRiskManager()
        
        # 模拟富途 API
        with patch('futu.OpenSecTradeContext') as mock_trd_ctx:
            mock_ret_data = MagicMock()
            mock_ret_data.iloc = [MagicMock()]
            mock_ret_data.iloc[0].__getitem__ = Mock(return_value='FUTU_ORDER_002')
            type(mock_ret_data).__len__ = Mock(return_value=1)
            
            mock_trd_ctx.return_value.place_order.return_value = (0, mock_ret_data)
            
            # 创建执行器
            executor = TradeExecutor()
            executor.position_manager = PositionManager(self.test_db_path)
            
            # 执行开仓（卖出）
            result = executor.execute(self.test_signal_sell, trade_type='OPEN')
            
            # 验证结果
            self.assertTrue(result.success)
            self.assertEqual(result.trade_type, 'OPEN')
            
            # 验证持仓
            position = executor.position_manager.get_position('US.AAPL')
            self.assertIsNotNone(position)
            self.assertEqual(position.action, 'SELL')
    
    @patch('app.trader.executor.config')
    @patch('app.trader.executor.get_risk_manager')
    def test_execute_alert_not_traded(self, mock_risk, mock_config):
        """测试 ALERT 类型不执行交易"""
        # 模拟风控管理器
        mock_risk.return_value = MockRiskManager()
        
        mock_config.OPEND_HOST = '127.0.0.1'
        
        executor = TradeExecutor()
        result = executor.execute(self.test_signal_alert, trade_type='OPEN')
        
        self.assertFalse(result.success)
        self.assertIn('ALERT', result.message)
        self.assertIsNone(result.position_id)
    
    @patch('app.trader.executor.config')
    @patch('app.trader.executor.get_risk_manager')
    def test_execute_close_success(self, mock_risk, mock_config):
        """测试平仓成功"""
        # 配置模拟
        mock_config.OPEND_HOST = '127.0.0.1'
        mock_config.OPEND_PORT = 11111
        mock_config.TRADE_ENV = 1
        mock_config.DEFAULT_ORDER_TYPE = 'MARKET'
        mock_config.MAX_ORDER_AMOUNT = 1000000
        
        # 模拟风控管理器
        mock_risk.return_value = MockRiskManager()
        
        # 模拟富途 API
        with patch('futu.OpenSecTradeContext') as mock_trd_ctx:
            mock_ret_data = MagicMock()
            mock_ret_data.iloc = [MagicMock()]
            mock_ret_data.iloc[0].__getitem__ = Mock(return_value='FUTU_ORDER_003')
            type(mock_ret_data).__len__ = Mock(return_value=1)
            
            mock_trd_ctx.return_value.place_order.return_value = (0, mock_ret_data)
            
            # 创建执行器
            executor = TradeExecutor()
            executor.position_manager = PositionManager(self.test_db_path)
            
            # 先开仓
            open_result = executor.execute(self.test_signal_buy, trade_type='OPEN')
            self.assertTrue(open_result.success)
            
            # 修改平仓信号价格（高于开仓价，买入应该盈利）
            close_signal = Signal(
                id="test_signal_close",
                code="US.AAPL",
                name="Apple Inc",
                action=SignalAction.SELL,
                price=185.00,  # 高于开仓价 175.50
                quantity=100,
                reason="获利平仓",
                strategy="测试策略"
            )
            
            # 执行平仓
            close_result = executor.execute(close_signal, trade_type='CLOSE')
            
            # 验证结果
            self.assertTrue(close_result.success)
            self.assertEqual(close_result.trade_type, 'CLOSE')
            self.assertEqual(close_result.position_id, open_result.position_id)
            self.assertIsNotNone(close_result.pnl)
            self.assertGreater(close_result.pnl, 0)  # 应该盈利
            
            # 验证持仓已关闭
            position = executor.position_manager.get_position('US.AAPL')
            self.assertIsNone(position)  # 应该没有 OPEN 状态的持仓
    
    @patch('app.trader.executor.config')
    @patch('app.trader.executor.get_risk_manager')
    def test_execute_close_no_position(self, mock_risk, mock_config):
        """测试平仓时没有持仓"""
        # 模拟风控管理器
        mock_risk.return_value = MockRiskManager()
        
        mock_config.OPEND_HOST = '127.0.0.1'
        
        executor = TradeExecutor()
        executor.position_manager = PositionManager(self.test_db_path)
        
        # 直接平仓（没有先开仓）
        result = executor.execute(self.test_signal_sell, trade_type='CLOSE')
        
        self.assertFalse(result.success)
        self.assertIn('未找到', result.message)
        self.assertIsNone(result.pnl)
    
    @patch('app.trader.executor.config')
    @patch('app.trader.executor.get_risk_manager')
    def test_execute_amount_limit(self, mock_risk, mock_config):
        """测试金额限制"""
        # 模拟风控管理器
        mock_risk.return_value = MockRiskManager()
        
        mock_config.OPEND_HOST = '127.0.0.1'
        mock_config.MAX_ORDER_AMOUNT = 1000  # 设置很小的限额
        
        executor = TradeExecutor()
        executor.position_manager = PositionManager(self.test_db_path)
        
        # 执行开仓（金额会超过限制）
        result = executor.execute(self.test_signal_buy, trade_type='OPEN')
        
        self.assertFalse(result.success)
        self.assertIn('超过限额', result.message)
    
    def test_trade_result_dataclass(self):
        """测试 TradeResult 数据类"""
        result = TradeResult(
            success=True,
            order_id='ORDER_001',
            position_id=123,
            trade_type='OPEN',
            pnl=500.00,
            message='测试成功'
        )
        
        self.assertTrue(result.success)
        self.assertEqual(result.order_id, 'ORDER_001')
        self.assertEqual(result.position_id, 123)
        self.assertEqual(result.trade_type, 'OPEN')
        self.assertEqual(result.pnl, 500.00)
        self.assertEqual(result.message, '测试成功')
    
    def test_trade_result_defaults(self):
        """测试 TradeResult 默认值"""
        result = TradeResult(success=False)
        
        self.assertFalse(result.success)
        self.assertIsNone(result.order_id)
        self.assertIsNone(result.position_id)
        self.assertIsNone(result.trade_type)
        self.assertIsNone(result.pnl)
        self.assertEqual(result.message, '')


if __name__ == '__main__':
    unittest.main(verbosity=2)
