"""
交易执行器超时测试

测试超时处理功能 (15 分钟超时自动标记为 expired)
"""
import unittest
import time
import threading
from datetime import datetime
import sys
import os

sys.path.insert(0, '/opt/ftseek/FTSeek')

from app.core.trade_executor import TradeExecutor, SignalStatus
from app.strategy.base import Signal, SignalAction
from app.storage.database import init_database


class MockDingTalkNotifier:
    """模拟钉钉通知器"""
    
    def __init__(self):
        self.sent_signals = []
        self.sent_texts = []
    
    def send_signal(self, signal: Signal) -> bool:
        self.sent_signals.append(signal)
        return True
    
    def send_text(self, content: str) -> bool:
        self.sent_texts.append(content)
        return True
    
    def send_execution_result(self, signal: Signal, success: bool, detail: str = ""):
        pass


class TestTimeout(unittest.TestCase):
    """超时处理测试"""
    
    def setUp(self):
        """测试前准备"""
        self.test_db_path = "/tmp/ftseek_timeout_test.db"
        self.db = init_database(self.test_db_path)
        self.mock_dingtalk = MockDingTalkNotifier()
        self.executor = TradeExecutor(self.mock_dingtalk)
        
        self.test_signal = Signal(
            id="test_timeout_001",
            code="HK.00700",
            name="腾讯控股",
            action=SignalAction.BUY,
            price=350.0,
            quantity=100,
            reason="测试超时",
            strategy="测试策略"
        )
    
    def tearDown(self):
        """测试后清理"""
        self.executor.cleanup()
        if os.path.exists(self.test_db_path):
            os.remove(self.test_db_path)
    
    def test_expire_timer_starts(self):
        """测试超时定时器启动"""
        # 提交信号
        result = self.executor.submit_signal(self.test_signal)
        self.assertTrue(result)
        
        # 验证定时器已启动
        self.assertIn(self.test_signal.id, self.executor._timers)
        timer = self.executor._timers[self.test_signal.id]
        self.assertTrue(timer.is_alive())
    
    def test_expire_timer_cancels_on_confirm(self):
        """测试确认后定时器取消"""
        # 提交信号
        self.executor.submit_signal(self.test_signal)
        
        # 确认
        self.executor.handle_confirm(self.test_signal.id, "user123")
        
        # 验证定时器已取消
        self.assertNotIn(self.test_signal.id, self.executor._timers)
    
    def test_expire_timer_cancels_on_ignore(self):
        """测试忽略后定时器取消"""
        # 提交信号
        self.executor.submit_signal(self.test_signal)
        
        # 忽略
        self.executor.handle_ignore(self.test_signal.id, "user123")
        
        # 验证定时器已取消
        self.assertNotIn(self.test_signal.id, self.executor._timers)
    
    def test_expire_handler(self):
        """测试过期处理逻辑 (手动触发)"""
        # 提交信号
        self.executor.submit_signal(self.test_signal)
        
        # 验证初始状态
        signal_data = self.db.get_signal(self.test_signal.id)
        self.assertEqual(signal_data['status'], SignalStatus.PENDING.value)
        
        # 手动触发过期处理
        self.executor._handle_expire(self.test_signal.id)
        
        # 验证状态已更新
        signal_data = self.db.get_signal(self.test_signal.id)
        self.assertEqual(signal_data['status'], SignalStatus.EXPIRED.value)
        
        # 验证发送了过期通知
        self.assertTrue(any("过期" in text for text in self.mock_dingtalk.sent_texts))
    
    def test_expire_only_affects_pending(self):
        """测试过期只影响 pending 状态的信号"""
        # 提交信号
        self.executor.submit_signal(self.test_signal)
        
        # 确认
        self.executor.handle_confirm(self.test_signal.id, "user123")
        
        # 手动触发过期处理 (应该无效)
        self.executor._handle_expire(self.test_signal.id)
        
        # 验证状态仍然是 executed
        signal_data = self.db.get_signal(self.test_signal.id)
        self.assertEqual(signal_data['status'], SignalStatus.EXECUTED.value)
    
    def test_short_timeout_simulation(self):
        """测试短超时模拟 (使用 1 秒超时)"""
        # 创建一个短超时的信号
        short_signal = Signal(
            id="test_short_timeout",
            code="HK.00700",
            action=SignalAction.BUY,
            price=350.0,
            quantity=100
        )
        
        # 修改 executor 的定时器时间为 1 秒
        original_start_timer = self.executor._start_expire_timer
        def short_start_timer(signal_id, delay_minutes=None):
            if delay_minutes is None:
                delay_minutes = 0.017  # 1 秒 (0.017 分钟)
            original_start_timer(signal_id, delay_minutes)
        
        self.executor._start_expire_timer = short_start_timer
        
        # 提交信号
        self.executor.submit_signal(short_signal)
        
        # 等待 2 秒
        time.sleep(2)
        
        # 验证状态已过期
        signal_data = self.db.get_signal(short_signal.id)
        self.assertEqual(signal_data['status'], SignalStatus.EXPIRED.value)


if __name__ == '__main__':
    unittest.main(verbosity=2)
