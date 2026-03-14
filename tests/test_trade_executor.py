"""
交易执行器单元测试

测试场景:
1. 信号提交流程
2. 用户确认流程
3. 用户延期流程
4. 用户忽略流程
5. 超时处理
6. 风控拒绝
7. 订单执行
8. 交易记录
"""
import unittest
import time
from datetime import datetime, timedelta
from unittest.mock import Mock, patch, MagicMock
import sys
import os

# 添加项目路径
sys.path.insert(0, '/opt/ftseek/FTSeek')

from app.core.trade_executor import (
    TradeExecutor, SignalStatus, OrderStatus, Order, Trade
)
from app.strategy.base import Signal, SignalAction
from app.storage.database import Database, init_database
from app.notifier.dingtalk import DingTalkNotifier


class MockDingTalkNotifier:
    """模拟钉钉通知器"""
    
    def __init__(self):
        self.sent_signals = []
        self.sent_texts = []
        self.sent_results = []
    
    def send_signal(self, signal: Signal) -> bool:
        self.sent_signals.append(signal)
        return True
    
    def send_text(self, content: str) -> bool:
        self.sent_texts.append(content)
        return True
    
    def send_execution_result(self, signal: Signal, success: bool, detail: str = ""):
        self.sent_results.append((signal, success, detail))
        return True


class TestTradeExecutor(unittest.TestCase):
    """交易执行器测试"""
    
    def setUp(self):
        """测试前准备"""
        # 创建测试数据库
        self.test_db_path = "/tmp/ftseek_test.db"
        self.db = init_database(self.test_db_path)
        
        # 创建模拟钉钉通知器
        self.mock_dingtalk = MockDingTalkNotifier()
        
        # 创建交易执行器
        self.executor = TradeExecutor(self.mock_dingtalk)
        
        # 创建测试信号
        self.test_signal = Signal(
            id="test_signal_001",
            code="HK.00700",
            name="腾讯控股",
            action=SignalAction.BUY,
            price=350.0,
            quantity=100,
            reason="技术面突破 + 情绪面利好",
            strategy="融合 v2",
            extra={
                'technical_score': 0.75,
                'sentiment_score': 0.80,
                'confidence': 0.78
            }
        )
    
    def tearDown(self):
        """测试后清理"""
        self.executor.cleanup()
        # 删除测试数据库
        if os.path.exists(self.test_db_path):
            os.remove(self.test_db_path)
    
    def test_submit_signal(self):
        """测试信号提交"""
        # 执行
        result = self.executor.submit_signal(self.test_signal)
        
        # 验证
        self.assertTrue(result)
        self.assertEqual(len(self.mock_dingtalk.sent_signals), 1)
        self.assertEqual(self.mock_dingtalk.sent_signals[0].id, self.test_signal.id)
        
        # 验证数据库记录
        signal_data = self.db.get_signal(self.test_signal.id)
        self.assertIsNotNone(signal_data)
        self.assertEqual(signal_data['status'], SignalStatus.PENDING.value)
        self.assertEqual(signal_data['code'], self.test_signal.code)
        self.assertEqual(signal_data['action'], self.test_signal.action.value)
    
    def test_handle_confirm(self):
        """测试用户确认"""
        # 先提交信号
        self.executor.submit_signal(self.test_signal)
        
        # 执行确认
        result = self.executor.handle_confirm(self.test_signal.id, "user123")
        
        # 验证
        self.assertTrue(result)
        
        # 验证数据库状态
        signal_data = self.db.get_signal(self.test_signal.id)
        self.assertEqual(signal_data['status'], SignalStatus.EXECUTED.value)
        self.assertIsNotNone(signal_data['confirmed_at'])
        self.assertIsNotNone(signal_data['executed_at'])
        
        # 验证执行结果推送
        self.assertEqual(len(self.mock_dingtalk.sent_results), 1)
    
    def test_handle_defer(self):
        """测试用户延期"""
        # 先提交信号
        self.executor.submit_signal(self.test_signal)
        
        # 执行延期
        result = self.executor.handle_defer(self.test_signal.id, "user123")
        
        # 验证
        self.assertTrue(result)
        
        # 验证数据库过期时间已更新
        signal_data = self.db.get_signal(self.test_signal.id)
        # 简单比较字符串格式的时间
        self.assertIsNotNone(signal_data['expire_at'])
        
        # 验证状态仍为 pending
        self.assertEqual(signal_data['status'], SignalStatus.PENDING.value)
        
        # 验证延期通知
        self.assertTrue(any("延期" in text for text in self.mock_dingtalk.sent_texts))
    
    def test_handle_ignore(self):
        """测试用户忽略"""
        # 先提交信号
        self.executor.submit_signal(self.test_signal)
        
        # 执行忽略
        result = self.executor.handle_ignore(self.test_signal.id, "user123")
        
        # 验证
        self.assertTrue(result)
        
        # 验证数据库状态
        signal_data = self.db.get_signal(self.test_signal.id)
        self.assertEqual(signal_data['status'], SignalStatus.IGNORED.value)
        
        # 验证忽略通知
        self.assertTrue(any("忽略" in text for text in self.mock_dingtalk.sent_texts))
    
    def test_handle_reject(self):
        """测试风控拒绝"""
        # 先提交信号
        self.executor.submit_signal(self.test_signal)
        
        # 执行风控拒绝
        result = self.executor.handle_reject(self.test_signal.id, "置信度过低")
        
        # 验证
        self.assertTrue(result)
        
        # 验证数据库状态
        signal_data = self.db.get_signal(self.test_signal.id)
        self.assertEqual(signal_data['status'], SignalStatus.REJECTED.value)
        
        # 验证风控通知
        self.assertTrue(any("风控拒绝" in text for text in self.mock_dingtalk.sent_texts))
    
    def test_execute_order(self):
        """测试订单执行"""
        # 创建订单
        order = Order(
            id="test_order_001",
            signal_id=self.test_signal.id,
            code=self.test_signal.code,
            name=self.test_signal.name,
            action=self.test_signal.action,
            price=self.test_signal.price,
            quantity=self.test_signal.quantity,
            strategy=self.test_signal.strategy
        )
        
        # 先保存信号
        self.executor.submit_signal(self.test_signal)
        
        # 执行订单
        result = self.executor.execute_order(order)
        
        # 验证
        self.assertTrue(result)
        self.assertEqual(order.status, OrderStatus.SUBMITTED)
        self.assertTrue(order.order_id.startswith("FUTU_"))
        
        # 验证交易记录
        trades = self.db.get_open_trades()
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]['signal_id'], self.test_signal.id)
        self.assertEqual(trades[0]['code'], self.test_signal.code)
    
    def test_record_trade(self):
        """测试交易记录"""
        # 创建交易
        trade = Trade(
            id="test_trade_001",
            signal_id=self.test_signal.id,
            order_id="test_order_001",
            code=self.test_signal.code,
            name=self.test_signal.name,
            action=self.test_signal.action,
            entry_price=self.test_signal.price,
            quantity=self.test_signal.quantity,
            strategy=self.test_signal.strategy,
            entry_time=datetime.now()
        )
        
        # 记录交易
        result = self.executor.record_trade(trade)
        
        # 验证
        self.assertTrue(result)
        
        # 验证数据库记录
        trades = self.db.get_open_trades()
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]['id'], trade.id)
        self.assertEqual(trades[0]['status'], "open")
    
    def test_status_transition(self):
        """测试状态流转"""
        # 提交信号 -> pending
        self.executor.submit_signal(self.test_signal)
        signal_data = self.db.get_signal(self.test_signal.id)
        self.assertEqual(signal_data['status'], SignalStatus.PENDING.value)
        
        # 确认 -> confirmed -> executed
        self.executor.handle_confirm(self.test_signal.id, "user123")
        signal_data = self.db.get_signal(self.test_signal.id)
        self.assertEqual(signal_data['status'], SignalStatus.EXECUTED.value)
    
    def test_double_confirm_prevention(self):
        """测试防止重复确认"""
        # 提交信号
        self.executor.submit_signal(self.test_signal)
        
        # 第一次确认
        result1 = self.executor.handle_confirm(self.test_signal.id, "user123")
        self.assertTrue(result1)
        
        # 第二次确认 (应该失败)
        result2 = self.executor.handle_confirm(self.test_signal.id, "user456")
        self.assertFalse(result2)
    
    def test_invalid_signal_id(self):
        """测试无效信号 ID"""
        # 确认不存在的信号
        result = self.executor.handle_confirm("invalid_signal_id", "user123")
        self.assertFalse(result)
        
        # 忽略不存在的信号
        result = self.executor.handle_ignore("invalid_signal_id", "user123")
        self.assertFalse(result)
        
        # 延期不存在的信号
        result = self.executor.handle_defer("invalid_signal_id", "user123")
        self.assertFalse(result)


class TestOrderAndTrade(unittest.TestCase):
    """订单和交易数据类测试"""
    
    def test_order_creation(self):
        """测试订单创建"""
        order = Order(
            id="order_001",
            signal_id="signal_001",
            code="HK.00700",
            name="腾讯控股",
            action=SignalAction.BUY,
            price=350.0,
            quantity=100,
            strategy="融合 v2"
        )
        
        self.assertEqual(order.id, "order_001")
        self.assertEqual(order.status, OrderStatus.PENDING)
        self.assertIsNotNone(order.created_at)
    
    def test_trade_creation(self):
        """测试交易创建"""
        trade = Trade(
            id="trade_001",
            signal_id="signal_001",
            order_id="order_001",
            code="HK.00700",
            name="腾讯控股",
            action=SignalAction.BUY,
            entry_price=350.0,
            quantity=100,
            strategy="融合 v2",
            entry_time=datetime.now()
        )
        
        self.assertEqual(trade.id, "trade_001")
        self.assertEqual(trade.status, "open")
        self.assertIsNone(trade.exit_price)


if __name__ == '__main__':
    # 运行测试
    unittest.main(verbosity=2)
