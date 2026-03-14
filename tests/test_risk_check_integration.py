"""
风控检查集成单元测试

测试 RiskManager.check_before_trade() 方法和 TradeExecutor 的风控集成
"""
import unittest
from unittest.mock import Mock, patch, MagicMock
import sys
import os
from datetime import datetime

# 添加项目路径
sys.path.insert(0, '/opt/ftseek/FTSeek')

from app.risk.risk_manager import RiskManager, RiskCheckResult, RiskConfig
from app.trader.executor import TradeExecutor, TradeResult
from app.strategy.base import Signal, SignalAction


class TestRiskCheckBeforeTrade(unittest.TestCase):
    """交易前风控检查测试"""
    
    def setUp(self):
        """测试前准备"""
        # 创建风控配置
        self.config = RiskConfig(
            min_confidence=0.60,
            max_single_position=0.20,
            max_daily_trades=10,
            max_daily_loss=0.05,
            max_drawdown=0.15,
            initial_cash=1000000.0
        )
        
        # 创建风控管理器
        self.risk_manager = RiskManager(limits=self.config)
        
        # 创建测试信号
        self.test_signal_high_confidence = Signal(
            id="test_001",
            code="US.AAPL",
            name="Apple Inc",
            action=SignalAction.BUY,
            price=175.50,
            quantity=100,
            reason="技术面突破",
            strategy="测试策略"
        )
        # 设置置信度 (通过 extra 字段)
        self.test_signal_high_confidence.extra['confidence'] = 0.85
        
        self.test_signal_low_confidence = Signal(
            id="test_002",
            code="US.AAPL",
            name="Apple Inc",
            action=SignalAction.BUY,
            price=175.50,
            quantity=100,
            reason="信号较弱",
            strategy="测试策略"
        )
        self.test_signal_low_confidence.extra['confidence'] = 0.45
    
    def test_check_before_trade_all_passed(self):
        """测试所有检查项通过"""
        # 设置账户状态 (健康状态)
        self.risk_manager.account = {
            "total_capital": 1000000.0,
            "available_cash": 800000.0,
            "positions": {},
            "peak_capital": 1000000.0
        }
        self.risk_manager.today_trades = 0
        self.risk_manager.today_pnl = 0.0
        
        result = self.risk_manager.check_before_trade(self.test_signal_high_confidence)
        
        self.assertIsInstance(result, RiskCheckResult)
        self.assertTrue(result.passed)
        self.assertIn('position_limit', result.details)
        self.assertIn('daily_trades', result.details)
        self.assertIn('daily_loss', result.details)
        self.assertIn('drawdown', result.details)
        self.assertIn('confidence', result.details)
        self.assertTrue(result.details['position_limit'])
        self.assertTrue(result.details['daily_trades'])
        self.assertTrue(result.details['daily_loss'])
        self.assertTrue(result.details['drawdown'])
        self.assertTrue(result.details['confidence'])
        self.assertIsNotNone(result.timestamp)
    
    def test_check_confidence_failed(self):
        """测试置信度检查失败"""
        self.risk_manager.account = {
            "total_capital": 1000000.0,
            "available_cash": 800000.0,
            "positions": {},
            "peak_capital": 1000000.0
        }
        self.risk_manager.today_trades = 0
        self.risk_manager.today_pnl = 0.0
        
        result = self.risk_manager.check_before_trade(self.test_signal_low_confidence)
        
        self.assertFalse(result.passed)
        self.assertFalse(result.details['confidence'])
    
    def test_check_position_limit_failed(self):
        """测试仓位限制检查失败"""
        # 设置大仓位场景
        self.risk_manager.account = {
            "total_capital": 1000000.0,
            "available_cash": 200000.0,
            "positions": {
                "US.AAPL": {
                    "quantity": 1000,
                    "entry_price": 175.00,
                    "current_price": 175.50
                }
            },
            "peak_capital": 1000000.0
        }
        self.risk_manager.today_trades = 0
        self.risk_manager.today_pnl = 0.0
        
        # 创建大仓位信号
        large_signal = Signal(
            id="test_large",
            code="US.AAPL",
            name="Apple Inc",
            action=SignalAction.BUY,
            price=175.50,
            quantity=500,  # 大额加仓
            reason="加仓",
            strategy="测试策略"
        )
        large_signal.extra['confidence'] = 0.85
        
        result = self.risk_manager.check_before_trade(large_signal)
        
        self.assertFalse(result.passed)
        self.assertFalse(result.details['position_limit'])
    
    def test_check_daily_trades_failed(self):
        """测试日交易次数检查失败"""
        self.risk_manager.account = {
            "total_capital": 1000000.0,
            "available_cash": 800000.0,
            "positions": {},
            "peak_capital": 1000000.0
        }
        self.risk_manager.today_trades = 10  # 达到限制
        self.risk_manager.today_pnl = 0.0
        
        result = self.risk_manager.check_before_trade(self.test_signal_high_confidence)
        
        self.assertFalse(result.passed)
        self.assertFalse(result.details['daily_trades'])
    
    def test_check_daily_loss_failed(self):
        """测试日亏损检查失败"""
        self.risk_manager.account = {
            "total_capital": 1000000.0,
            "available_cash": 800000.0,
            "positions": {},
            "peak_capital": 1000000.0
        }
        self.risk_manager.today_trades = 5
        self.risk_manager.today_pnl = -60000.0  # 亏损 6%
        
        result = self.risk_manager.check_before_trade(self.test_signal_high_confidence)
        
        self.assertFalse(result.passed)
        self.assertFalse(result.details['daily_loss'])
    
    def test_check_drawdown_failed(self):
        """测试回撤检查失败"""
        self.risk_manager.account = {
            "total_capital": 850000.0,  # 从 100 万回撤到 85 万
            "available_cash": 850000.0,
            "positions": {},
            "peak_capital": 1000000.0
        }
        self.risk_manager.today_trades = 0
        self.risk_manager.today_pnl = 0.0
        
        result = self.risk_manager.check_before_trade(self.test_signal_high_confidence)
        
        self.assertFalse(result.passed)
        self.assertFalse(result.details['drawdown'])
        # 回撤 = (1000000 - 850000) / 1000000 = 15%
    
    def test_check_before_trade_exception_handling(self):
        """测试异常处理"""
        # 创建一个有问题的信号
        bad_signal = Mock()
        bad_signal.code = None
        bad_signal.quantity = None
        bad_signal.extra = {}
        
        # 应该返回失败而不是抛出异常
        result = self.risk_manager.check_before_trade(bad_signal)
        
        self.assertIsInstance(result, RiskCheckResult)
        self.assertFalse(result.passed)
    
    def test_check_confidence_from_extra(self):
        """测试从 extra 字段获取置信度"""
        self.risk_manager.account = {
            "total_capital": 1000000.0,
            "available_cash": 800000.0,
            "positions": {},
            "peak_capital": 1000000.0
        }
        self.risk_manager.today_trades = 0
        self.risk_manager.today_pnl = 0.0
        
        # 信号没有 confidence 属性，但有 extra.confidence
        signal = Signal(
            id="test_extra",
            code="US.AAPL",
            name="Apple Inc",
            action=SignalAction.BUY,
            price=175.50,
            quantity=100,
            reason="测试",
            strategy="测试策略"
        )
        signal.extra['confidence'] = 0.75
        
        result = self.risk_manager.check_before_trade(signal)
        
        self.assertTrue(result.passed)
        self.assertTrue(result.details['confidence'])
    
    def test_check_confidence_default_zero(self):
        """测试没有置信度时默认为 0"""
        self.risk_manager.account = {
            "total_capital": 1000000.0,
            "available_cash": 800000.0,
            "positions": {},
            "peak_capital": 1000000.0
        }
        self.risk_manager.today_trades = 0
        self.risk_manager.today_pnl = 0.0
        
        # 信号没有任何置信度信息
        signal = Signal(
            id="test_no_conf",
            code="US.AAPL",
            name="Apple Inc",
            action=SignalAction.BUY,
            price=175.50,
            quantity=100,
            reason="测试",
            strategy="测试策略"
        )
        
        result = self.risk_manager.check_before_trade(signal)
        
        self.assertFalse(result.passed)
        self.assertFalse(result.details['confidence'])


class TestTradeExecutorRiskIntegration(unittest.TestCase):
    """TradeExecutor 风控集成测试"""
    
    def setUp(self):
        """测试前准备"""
        # 创建测试信号
        self.test_signal = Signal(
            id="test_signal",
            code="US.AAPL",
            name="Apple Inc",
            action=SignalAction.BUY,
            price=175.50,
            quantity=100,
            reason="技术面突破",
            strategy="测试策略"
        )
        self.test_signal.extra['confidence'] = 0.85
    
    @patch('app.trader.executor.get_risk_manager')
    @patch('app.trader.executor.config')
    def test_execute_open_risk_check_passed(self, mock_config, mock_get_risk):
        """测试开仓时风控检查通过"""
        # 模拟风控管理器
        mock_risk_manager = Mock()
        mock_risk_manager.check_before_trade.return_value = RiskCheckResult(
            passed=True,
            details={
                'position_limit': True,
                'daily_trades': True,
                'daily_loss': True,
                'drawdown': True,
                'confidence': True
            },
            timestamp=datetime.now()
        )
        mock_get_risk.return_value = mock_risk_manager
        
        # 配置模拟
        mock_config.OPEND_HOST = '127.0.0.1'
        mock_config.OPEND_PORT = 11111
        mock_config.TRADE_ENV = 1
        mock_config.DEFAULT_ORDER_TYPE = 'MARKET'
        mock_config.DEFAULT_ORDER_QTY_US = 100
        mock_config.MAX_ORDER_AMOUNT = 1000000
        
        # 模拟富途 API 成功
        with patch('futu.OpenSecTradeContext') as mock_trd_ctx:
            from unittest.mock import MagicMock
            mock_ret_data = MagicMock()
            mock_ret_data.iloc = [MagicMock()]
            mock_ret_data.iloc[0].__getitem__ = Mock(return_value='FUTU_ORDER_001')
            type(mock_ret_data).__len__ = Mock(return_value=1)
            mock_trd_ctx.return_value.place_order.return_value = (0, mock_ret_data)
            
            # 创建执行器
            executor = TradeExecutor()
            
            # 执行开仓
            result = executor.execute(self.test_signal, trade_type='OPEN')
            
            # 验证风控检查被调用
            mock_risk_manager.check_before_trade.assert_called_once_with(self.test_signal)
            
            # 验证交易执行成功
            self.assertTrue(result.success)
            self.assertEqual(result.trade_type, 'OPEN')
    
    @patch('app.trader.executor.get_risk_manager')
    @patch('app.trader.executor.config')
    def test_execute_open_risk_check_failed(self, mock_config, mock_get_risk):
        """测试开仓时风控检查失败"""
        # 模拟风控检查失败
        mock_risk_manager = Mock()
        mock_risk_manager.check_before_trade.return_value = RiskCheckResult(
            passed=False,
            details={
                'position_limit': False,
                'daily_trades': True,
                'daily_loss': True,
                'drawdown': True,
                'confidence': True
            },
            timestamp=datetime.now()
        )
        mock_get_risk.return_value = mock_risk_manager
        
        # 配置模拟 (不需要富途 API)
        mock_config.OPEND_HOST = '127.0.0.1'
        
        # 创建执行器
        executor = TradeExecutor()
        
        # 执行开仓
        result = executor.execute(self.test_signal, trade_type='OPEN')
        
        # 验证风控检查被调用
        mock_risk_manager.check_before_trade.assert_called_once_with(self.test_signal)
        
        # 验证交易被拒绝
        self.assertFalse(result.success)
        self.assertIn('风控检查失败', result.message)
        self.assertIn('position_limit', result.message)
        self.assertIsNone(result.position_id)
        self.assertIsNone(result.order_id)
    
    @patch('app.trader.executor.get_risk_manager')
    @patch('app.trader.executor.config')
    def test_execute_close_skip_risk_check(self, mock_config, mock_get_risk):
        """测试平仓时跳过风控检查"""
        # 模拟风控管理器
        mock_risk_manager = Mock()
        mock_get_risk.return_value = mock_risk_manager
        
        # 配置模拟
        mock_config.OPEND_HOST = '127.0.0.1'
        mock_config.MAX_ORDER_AMOUNT = 1000000
        
        # 创建执行器
        executor = TradeExecutor()
        executor.position_manager = Mock()
        executor.position_manager.get_position.return_value = None
        
        # 执行平仓
        result = executor.execute(self.test_signal, trade_type='CLOSE')
        
        # 验证风控检查没有被调用
        mock_risk_manager.check_before_trade.assert_not_called()
        
        # 验证因为找不到持仓而失败
        self.assertFalse(result.success)
        self.assertIn('未找到', result.message)
    
    @patch('app.trader.executor.get_risk_manager')
    @patch('app.trader.executor.config')
    def test_execute_alert_skip_risk_check(self, mock_config, mock_get_risk):
        """测试 ALERT 类型信号跳过风控检查"""
        # 模拟风控管理器
        mock_risk_manager = Mock()
        mock_get_risk.return_value = mock_risk_manager
        
        # 配置模拟
        mock_config.OPEND_HOST = '127.0.0.1'
        
        # 创建 ALERT 类型信号
        alert_signal = Signal(
            id="test_alert",
            code="US.AAPL",
            name="Apple Inc",
            action=SignalAction.ALERT,
            price=175.50,
            quantity=100,
            reason="仅提醒",
            strategy="测试策略"
        )
        
        # 创建执行器
        executor = TradeExecutor()
        
        # 执行信号
        result = executor.execute(alert_signal, trade_type='OPEN')
        
        # 验证风控检查没有被调用
        mock_risk_manager.check_before_trade.assert_not_called()
        
        # 验证因为 ALERT 类型而拒绝
        self.assertFalse(result.success)
        self.assertIn('ALERT', result.message)
    
    @patch('app.trader.executor.get_risk_manager')
    @patch('app.trader.executor.config')
    def test_execute_multiple_risk_checks_failed(self, mock_config, mock_get_risk):
        """测试多个风控检查项失败"""
        # 模拟多个检查项失败
        mock_risk_manager = Mock()
        mock_risk_manager.check_before_trade.return_value = RiskCheckResult(
            passed=False,
            details={
                'position_limit': False,
                'daily_trades': False,
                'daily_loss': True,
                'drawdown': False,
                'confidence': True
            },
            timestamp=datetime.now()
        )
        mock_get_risk.return_value = mock_risk_manager
        
        mock_config.OPEND_HOST = '127.0.0.1'
        
        executor = TradeExecutor()
        result = executor.execute(self.test_signal, trade_type='OPEN')
        
        # 验证错误信息包含所有失败的检查项
        self.assertFalse(result.success)
        self.assertIn('风控检查失败', result.message)
        self.assertIn('position_limit', result.message)
        self.assertIn('daily_trades', result.message)
        self.assertIn('drawdown', result.message)


class TestRiskCheckResultDataclass(unittest.TestCase):
    """RiskCheckResult 数据类测试"""
    
    def test_risk_check_result_creation(self):
        """测试 RiskCheckResult 创建"""
        result = RiskCheckResult(
            passed=True,
            details={
                'position_limit': True,
                'daily_trades': True,
            },
            timestamp=datetime.now()
        )
        
        self.assertTrue(result.passed)
        self.assertEqual(len(result.details), 2)
        self.assertIsNotNone(result.timestamp)
    
    def test_risk_check_result_defaults(self):
        """测试 RiskCheckResult 默认值"""
        result = RiskCheckResult(passed=False)
        
        self.assertFalse(result.passed)
        self.assertEqual(result.details, {})
        self.assertIsNone(result.timestamp)


if __name__ == '__main__':
    unittest.main(verbosity=2)
