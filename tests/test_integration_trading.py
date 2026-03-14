"""
集成测试 - 完整交易流程验证

测试目标:
1. 开仓→平仓完整流程
2. 风控拦截超限交易
3. 推送消息格式验证
4. 测试独立可重复运行

约束:
- 使用模拟数据，不依赖真实富途连接
- 测试要独立，可重复运行
- 清理测试数据
"""
import pytest
import sqlite3
import os
import tempfile
from datetime import datetime
from unittest.mock import Mock, patch, MagicMock
import sys

# 添加项目路径
sys.path.insert(0, '/opt/ftseek/FTSeek')

from app.trader.executor import TradeExecutor, TradeResult
from app.trader.position_manager import PositionManager
from app.strategy.base import Signal, SignalAction


class TestIntegrationTrading:
    """集成测试 - 完整交易流程"""
    
    @pytest.fixture
    def temp_db(self):
        """创建临时数据库用于测试"""
        fd, path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        yield path
        # 清理临时数据库
        if os.path.exists(path):
            os.remove(path)
    
    @pytest.fixture
    def mock_futu_module(self):
        """模拟富途模块"""
        mock_futu = MagicMock()
        
        # 模拟 TrdSide, OrderType, TrdEnv
        mock_futu.TrdSide.BUY = "BUY"
        mock_futu.TrdSide.SELL = "SELL"
        mock_futu.OrderType.MARKET = "MARKET"
        mock_futu.OrderType.NORMAL = "NORMAL"
        mock_futu.TrdEnv.SIMULATE = "SIMULATE"
        mock_futu.TrdEnv.REAL = "REAL"
        mock_futu.TrdMarket.HK = "HK"
        mock_futu.TrdMarket.US = "US"
        
        # 模拟 OpenSecTradeContext
        mock_ctx = MagicMock()
        # place_order 返回 (ret=0, data)
        mock_data = MagicMock()
        mock_data.__len__ = lambda self: 1
        mock_data.iloc = [MagicMock()]
        mock_data.iloc[0].__getitem__ = lambda s, key: 12345678 if key == "order_id" else None
        mock_ctx.place_order.return_value = (0, mock_data)
        mock_ctx.close = MagicMock()
        
        mock_futu.OpenSecTradeContext.return_value = mock_ctx
        
        with patch.dict('sys.modules', {'futu': mock_futu}):
            yield mock_futu
    
    @pytest.fixture
    def mock_config(self):
        """模拟配置"""
        with patch('app.trader.executor.config') as mock_cfg:
            mock_cfg.OPEND_HOST = "127.0.0.1"
            mock_cfg.OPEND_PORT = 11111
            mock_cfg.TRADE_ENV = 1  # 模拟环境
            mock_cfg.DEFAULT_ORDER_QTY_HK = 100
            mock_cfg.DEFAULT_ORDER_QTY_US = 1
            mock_cfg.DEFAULT_ORDER_TYPE = "MARKET"
            mock_cfg.MAX_ORDER_AMOUNT = 50000.0
            yield mock_cfg
    
    @pytest.fixture
    def mock_risk_manager(self):
        """模拟风控管理器 - 默认通过所有检查"""
        mock_rm = MagicMock()
        mock_result = MagicMock()
        mock_result.passed = True
        mock_result.details = {'confidence': True, 'position_limit': True, 'amount_limit': True}
        mock_rm.check_before_trade.return_value = mock_result
        
        with patch('app.trader.executor.get_risk_manager') as mock_get_rm:
            mock_get_rm.return_value = mock_rm
            yield mock_rm
    
    @pytest.fixture
    def setup_test_env(self, temp_db, mock_config, mock_futu_module, mock_risk_manager):
        """准备测试环境 (数据库 + mock)"""
        # 设置临时数据库路径
        with patch.object(PositionManager, '__init__', lambda self, db_path=None: None):
            pm = PositionManager()
            pm.db_path = temp_db
            pm._ensure_table()
        
        # 创建使用临时数据库的 executor
        with patch('app.trader.executor.PositionManager') as MockPM:
            mock_pm = PositionManager(db_path=temp_db)
            MockPM.return_value = mock_pm
            yield temp_db
    
    def _create_signal(self, code, action, quantity, price, reason="Test"):
        """Helper to create signals with confidence"""
        return Signal(
            code=code,
            action=action,
            quantity=quantity,
            price=price,
            reason=reason,
            extra={'confidence': 0.85}
        )
    
    def test_open_position_flow(self, setup_test_env, mock_futu_module, mock_risk_manager):
        """测试开仓流程"""
        # 1. 创建买入信号
        signal = self._create_signal('HK.00700', SignalAction.BUY, 100, 420.00, "Test open")
        
        # 2. 执行交易 (使用临时数据库)
        with patch('app.trader.executor.PositionManager') as MockPM:
            mock_pm = PositionManager(db_path=setup_test_env)
            MockPM.return_value = mock_pm
            
            executor = TradeExecutor()
            result = executor.execute(signal, trade_type='OPEN')
        
        # 3. 验证
        assert result.success is True
        assert result.order_id is not None
        assert result.position_id is not None
        assert result.position_id > 0
        assert result.trade_type == 'OPEN'
        
        # 4. 验证数据库记录
        pm = PositionManager(db_path=setup_test_env)
        position = pm.get_position('HK.00700')
        assert position is not None
        assert position.status == 'OPEN'
        assert position.quantity == 100
        assert position.entry_price == 420.00
        assert position.action == 'BUY'
    
    def test_close_position_on_reverse_signal(self, setup_test_env, mock_futu_module, mock_risk_manager):
        """测试反向信号平仓"""
        # 1. 先开仓
        buy_signal = self._create_signal('HK.00700', SignalAction.BUY, 100, 420.00, "Test buy")
        
        with patch('app.trader.executor.PositionManager') as MockPM:
            mock_pm = PositionManager(db_path=setup_test_env)
            MockPM.return_value = mock_pm
            
            executor = TradeExecutor()
            open_result = executor.execute(buy_signal, 'OPEN')
        
        assert open_result.success is True
        assert open_result.position_id is not None
        
        # 2. 反向信号 (平仓)
        sell_signal = self._create_signal('HK.00700', SignalAction.SELL, 100, 450.00, "Test sell")
        
        with patch('app.trader.executor.PositionManager') as MockPM:
            mock_pm = PositionManager(db_path=setup_test_env)
            MockPM.return_value = mock_pm
            
            executor = TradeExecutor()
            close_result = executor.execute(sell_signal, trade_type='CLOSE')
        
        # 3. 验证
        assert close_result.success is True
        assert close_result.order_id is not None
        assert close_result.trade_type == 'CLOSE'
        assert close_result.pnl is not None
        # 买入平仓盈利：(450.00 - 420.00) * 100 = 3000
        assert close_result.pnl == 3000.00
        
        # 4. 验证持仓已关闭
        pm = PositionManager(db_path=setup_test_env)
        position = pm.get_position('HK.00700')
        assert position is None  # status 变为 CLOSED，get_position 返回 None
        
        # 验证数据库中状态已更新
        conn = sqlite3.connect(setup_test_env)
        cursor = conn.cursor()
        cursor.execute('SELECT status, exit_price, realized_pnl FROM positions WHERE code = ?', ('HK.00700',))
        row = cursor.fetchone()
        conn.close()
        
        assert row[0] == 'CLOSED'
        assert row[1] == 450.00
        assert row[2] == 3000.00
    
    def test_risk_check_blocks_over_limit(self, setup_test_env, mock_config, mock_futu_module):
        """测试风控拦截超限交易"""
        # 禁用风控 mock，测试真实的风控逻辑
        # 设置超限场景：价格 * 数量 > MAX_ORDER_AMOUNT
        # MAX_ORDER_AMOUNT = 50000, 设置 price=600, qty=100 → 60000 > 50000
        signal = self._create_signal('HK.00700', SignalAction.BUY, 100, 600.00, "Test risk limit")
        
        # 更新 mock 配置的限额
        mock_config.MAX_ORDER_AMOUNT = 50000.0
        
        # 设置风控检查通过 (只测试金额限制，不测试 confidence)
        mock_rm = MagicMock()
        mock_result = MagicMock()
        mock_result.passed = True
        mock_result.details = {'confidence': True, 'position_limit': True, 'amount_limit': True}
        mock_rm.check_before_trade.return_value = mock_result
        
        with patch('app.trader.executor.get_risk_manager') as mock_get_rm:
            mock_get_rm.return_value = mock_rm
            
            with patch('app.trader.executor.PositionManager') as MockPM:
                mock_pm = PositionManager(db_path=setup_test_env)
                MockPM.return_value = mock_pm
                
                executor = TradeExecutor()
                result = executor.execute(signal, trade_type='OPEN')
        
        # 验证交易被拒绝 (金额超限)
        assert result.success is False
        assert "超过限额" in result.message
        assert result.order_id is None
        assert result.position_id is None
        
        # 验证没有创建持仓记录
        pm = PositionManager(db_path=setup_test_env)
        position = pm.get_position('HK.00700')
        assert position is None
    
    def test_detailed_push_message_format(self, setup_test_env, mock_futu_module, mock_risk_manager):
        """测试推送消息格式"""
        # 这个测试验证 TradeResult 包含推送所需的所有字段
        
        # 1. 测试开仓推送字段
        signal = self._create_signal('HK.00700', SignalAction.BUY, 100, 420.00, "Test push")
        
        with patch('app.trader.executor.PositionManager') as MockPM:
            mock_pm = PositionManager(db_path=setup_test_env)
            MockPM.return_value = mock_pm
            
            executor = TradeExecutor()
            open_result = executor.execute(signal, trade_type='OPEN')
        
        # 验证开仓结果包含所有必需字段
        assert hasattr(open_result, 'success')
        assert hasattr(open_result, 'order_id')
        assert hasattr(open_result, 'position_id')
        assert hasattr(open_result, 'trade_type')
        assert hasattr(open_result, 'message')
        
        assert open_result.success is True
        assert open_result.order_id is not None
        assert open_result.position_id is not None
        assert open_result.trade_type == 'OPEN'
        assert '开仓' in open_result.message or 'OPEN' in open_result.message
        
        # 2. 测试平仓推送字段 (包含盈亏信息)
        sell_signal = self._create_signal('HK.00700', SignalAction.SELL, 100, 450.00, "Test close")
        
        with patch('app.trader.executor.PositionManager') as MockPM:
            mock_pm = PositionManager(db_path=setup_test_env)
            MockPM.return_value = mock_pm
            
            executor = TradeExecutor()
            close_result = executor.execute(sell_signal, trade_type='CLOSE')
        
        # 验证平仓结果包含盈亏信息
        assert hasattr(close_result, 'pnl')
        assert close_result.success is True
        assert close_result.pnl is not None
        assert close_result.trade_type == 'CLOSE'
        assert 'pnl' in close_result.message.lower() or '盈亏' in close_result.message or str(close_result.pnl) in close_result.message
    
    def test_sell_short_flow(self, setup_test_env, mock_futu_module, mock_risk_manager):
        """测试卖出 (做空) 开仓流程"""
        # 1. 创建卖出信号 (做空)
        signal = self._create_signal('HK.00700', SignalAction.SELL, 100, 500.00, "Test short")
        
        with patch('app.trader.executor.PositionManager') as MockPM:
            mock_pm = PositionManager(db_path=setup_test_env)
            MockPM.return_value = mock_pm
            
            executor = TradeExecutor()
            result = executor.execute(signal, trade_type='OPEN')
        
        # 2. 验证
        assert result.success is True
        assert result.position_id is not None
        
        # 3. 验证持仓记录
        pm = PositionManager(db_path=setup_test_env)
        position = pm.get_position('HK.00700')
        assert position is not None
        assert position.action == 'SELL'
        assert position.quantity == 100
        assert position.entry_price == 500.00
        
        # 4. 平仓 (价格下跌，做空盈利)
        buy_back_signal = self._create_signal('HK.00700', SignalAction.BUY, 100, 480.00, "Test buyback")
        
        with patch('app.trader.executor.PositionManager') as MockPM:
            mock_pm = PositionManager(db_path=setup_test_env)
            MockPM.return_value = mock_pm
            
            executor = TradeExecutor()
            close_result = executor.execute(buy_back_signal, trade_type='CLOSE')
        
        # 5. 验证做空盈利：(500.00 - 480.00) * 100 = 2000
        assert close_result.success is True
        assert close_result.pnl == 2000.00
    
    def test_close_non_existent_position(self, setup_test_env, mock_futu_module, mock_risk_manager):
        """测试平仓不存在的持仓"""
        # 直接平仓，没有先开仓
        signal = self._create_signal('HK.00700', SignalAction.SELL, 100, 420.00, "Test close none")
        
        with patch('app.trader.executor.PositionManager') as MockPM:
            mock_pm = PositionManager(db_path=setup_test_env)
            MockPM.return_value = mock_pm
            
            executor = TradeExecutor()
            result = executor.execute(signal, trade_type='CLOSE')
        
        # 验证失败
        assert result.success is False
        assert "未找到" in result.message or "持仓" in result.message
    
    def test_alert_signal_not_executed(self, setup_test_env):
        """测试 ALERT 类型信号不执行交易"""
        signal = Signal(
            code='HK.00700',
            action=SignalAction.ALERT,
            quantity=100,
            price=420.00,
            reason="Test alert"
        )
        
        executor = TradeExecutor()
        result = executor.execute(signal, trade_type='OPEN')
        
        # ALERT 类型应该被拒绝
        assert result.success is False
        assert "ALERT" in result.message
    
    def test_multiple_positions_independent(self, setup_test_env, mock_futu_module, mock_risk_manager):
        """测试多个持仓独立管理"""
        with patch('app.trader.executor.PositionManager') as MockPM:
            mock_pm = PositionManager(db_path=setup_test_env)
            MockPM.return_value = mock_pm
            
            executor = TradeExecutor()
            
            # 开仓 1
            signal1 = self._create_signal('HK.00700', SignalAction.BUY, 100, 420.00, "Test pos1")
            result1 = executor.execute(signal1, trade_type='OPEN')
            
            # 开仓 2
            signal2 = self._create_signal('HK.09988', SignalAction.BUY, 200, 150.00, "Test pos2")
            result2 = executor.execute(signal2, trade_type='OPEN')
            
            assert result1.success is True
            assert result2.success is True
            assert result1.position_id != result2.position_id
        
        # 验证两个持仓都存在
        pm = PositionManager(db_path=setup_test_env)
        pos1 = pm.get_position('HK.00700')
        pos2 = pm.get_position('HK.09988')
        
        assert pos1 is not None
        assert pos2 is not None
        assert pos1.code == 'HK.00700'
        assert pos2.code == 'HK.09988'
    
    def test_position_pnl_calculation_accuracy(self, setup_test_env, mock_futu_module, mock_risk_manager):
        """测试盈亏计算准确性"""
        test_cases = [
            # (action, entry_price, exit_price, quantity, expected_pnl)
            ('BUY', 100.0, 120.0, 100, 2000.0),   # 买入盈利
            ('BUY', 100.0, 80.0, 100, -2000.0),   # 买入亏损
            ('SELL', 100.0, 80.0, 100, 2000.0),   # 卖出盈利
            ('SELL', 100.0, 120.0, 100, -2000.0), # 卖出亏损
            ('BUY', 100.0, 100.0, 100, 0.0),      # 不赚不亏
        ]
        
        for action, entry_price, exit_price, quantity, expected_pnl in test_cases:
            # 清空数据库
            conn = sqlite3.connect(setup_test_env)
            cursor = conn.cursor()
            cursor.execute('DELETE FROM positions')
            conn.commit()
            conn.close()
            
            # 开仓
            signal_action = SignalAction.BUY if action == 'BUY' else SignalAction.SELL
            open_signal = self._create_signal('HK.00001', signal_action, quantity, entry_price, f"Test {action}")
            
            with patch('app.trader.executor.PositionManager') as MockPM:
                mock_pm = PositionManager(db_path=setup_test_env)
                MockPM.return_value = mock_pm
                
                executor = TradeExecutor()
                open_result = executor.execute(open_signal, trade_type='OPEN')
                assert open_result.success is True
            
            # 平仓
            close_signal = self._create_signal('HK.00001', signal_action, quantity, exit_price, f"Test close {action}")
            
            with patch('app.trader.executor.PositionManager') as MockPM:
                mock_pm = PositionManager(db_path=setup_test_env)
                MockPM.return_value = mock_pm
                
                executor = TradeExecutor()
                close_result = executor.execute(close_signal, trade_type='CLOSE')
                assert close_result.success is True
            
            # 验证盈亏
            assert close_result.pnl == expected_pnl, f"{action} 场景：期望 {expected_pnl}, 实际 {close_result.pnl}"


class TestIntegrationEdgeCases:
    """边界情况集成测试"""
    
    @pytest.fixture
    def temp_db(self):
        """创建临时数据库"""
        fd, path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        yield path
        if os.path.exists(path):
            os.remove(path)
    
    def test_concurrent_position_same_code(self, temp_db):
        """测试同一标的多次开仓 (应该是替换而不是叠加)"""
        # 注意：当前实现 get_position 只返回一个 OPEN 持仓
        # 多次开仓同一标的会创建多条记录，但 get_position 只返回第一条
        
        pm = PositionManager(db_path=temp_db)
        
        # 第一次开仓
        pm.create_position('HK.00700', 'BUY', 100, 420.0, '2024-01-01 10:00:00')
        
        # 第二次开仓同一标的
        pm.create_position('HK.00700', 'BUY', 200, 430.0, '2024-01-02 10:00:00')
        
        # get_position 只返回第一条
        position = pm.get_position('HK.00700')
        assert position is not None
        assert position.quantity == 100  # 第一条的数量
        
        # get_all_positions 返回所有
        all_positions = pm.get_all_positions()
        assert len(all_positions) == 2
    
    def test_database_cleanup_after_test(self, temp_db):
        """测试数据库清理"""
        pm = PositionManager(db_path=temp_db)
        
        # 创建测试数据
        pm.create_position('HK.00700', 'BUY', 100, 420.0, '2024-01-01 10:00:00')
        
        # 验证数据存在
        position = pm.get_position('HK.00700')
        assert position is not None
        
        # 清理 (手动删除文件)
        conn = sqlite3.connect(temp_db)
        conn.execute('DELETE FROM positions')
        conn.commit()
        conn.close()
        
        # 验证数据已清理
        position = pm.get_position('HK.00700')
        assert position is None
    
    def test_empty_signal_code(self, temp_db):
        """测试空代码信号"""
        signal = Signal(
            code='',
            action=SignalAction.BUY,
            quantity=100,
            price=420.0,
            reason="Test empty"
        )
        
        with patch('app.trader.executor.PositionManager') as MockPM:
            mock_pm = PositionManager(db_path=temp_db)
            MockPM.return_value = mock_pm
            
            executor = TradeExecutor()
            result = executor.execute(signal, trade_type='OPEN')
        
        # 应该失败或处理异常
        assert result.success is False or result.order_id is None


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
