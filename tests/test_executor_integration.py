"""
交易执行器集成测试

测试完整流程：
信号生成 → 风控检查 → 推送钉钉 → 等待确认 → 执行/跳过
"""
import unittest
from datetime import datetime
import sys
import os

sys.path.insert(0, '/opt/ftseek/FTSeek')

from app.core.trade_executor import TradeExecutor, SignalStatus, OrderStatus
from app.strategy.base import Signal, SignalAction
from app.storage.database import init_database


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


class TestIntegration(unittest.TestCase):
    """集成测试"""
    
    def setUp(self):
        """测试前准备"""
        self.test_db_path = "/tmp/ftseek_integration_test.db"
        self.db = init_database(self.test_db_path)
        self.mock_dingtalk = MockDingTalkNotifier()
        self.executor = TradeExecutor(self.mock_dingtalk)
    
    def tearDown(self):
        """测试后清理"""
        self.executor.cleanup()
        if os.path.exists(self.test_db_path):
            os.remove(self.test_db_path)
    
    def test_full_success_flow(self):
        """测试完整成功流程"""
        # 1. 创建信号
        signal = Signal(
            id="integration_001",
            code="HK.00700",
            name="腾讯控股",
            action=SignalAction.BUY,
            price=350.0,
            quantity=100,
            reason="技术面突破",
            strategy="融合 v2"
        )
        
        # 2. 提交信号
        submit_result = self.executor.submit_signal(signal)
        self.assertTrue(submit_result)
        
        # 验证：信号已保存
        signal_data = self.db.get_signal(signal.id)
        self.assertEqual(signal_data['status'], SignalStatus.PENDING.value)
        
        # 验证：钉钉推送
        self.assertEqual(len(self.mock_dingtalk.sent_signals), 1)
        
        # 3. 用户确认
        confirm_result = self.executor.handle_confirm(signal.id, "trader_001")
        self.assertTrue(confirm_result)
        
        # 验证：信号已执行
        signal_data = self.db.get_signal(signal.id)
        self.assertEqual(signal_data['status'], SignalStatus.EXECUTED.value)
        self.assertIsNotNone(signal_data['confirmed_at'])
        self.assertIsNotNone(signal_data['executed_at'])
        
        # 验证：交易记录
        trades = self.db.get_open_trades()
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]['signal_id'], signal.id)
        self.assertEqual(trades[0]['code'], signal.code)
        self.assertEqual(trades[0]['action'], signal.action.value)
        
        # 验证：执行结果推送
        self.assertEqual(len(self.mock_dingtalk.sent_results), 1)
    
    def test_ignore_flow(self):
        """测试忽略流程"""
        signal = Signal(
            id="integration_002",
            code="HK.09988",
            name="阿里巴巴",
            action=SignalAction.SELL,
            price=80.0,
            quantity=200,
            reason="技术面破位",
            strategy="均线交叉"
        )
        
        # 提交
        self.executor.submit_signal(signal)
        
        # 忽略
        self.executor.handle_ignore(signal.id, "trader_002")
        
        # 验证状态
        signal_data = self.db.get_signal(signal.id)
        self.assertEqual(signal_data['status'], SignalStatus.IGNORED.value)
        
        # 验证：没有交易记录
        trades = self.db.get_open_trades()
        self.assertEqual(len(trades), 0)
    
    def test_defer_then_confirm_flow(self):
        """测试延期后确认流程"""
        signal = Signal(
            id="integration_003",
            code="US.AAPL",
            name="苹果",
            action=SignalAction.BUY,
            price=180.0,
            quantity=50,
            reason="财报利好",
            strategy="RSI 反弹"
        )
        
        # 提交
        self.executor.submit_signal(signal)
        
        # 延期
        defer_result = self.executor.handle_defer(signal.id, "trader_003")
        self.assertTrue(defer_result)
        
        # 验证：状态仍为 pending
        signal_data = self.db.get_signal(signal.id)
        self.assertEqual(signal_data['status'], SignalStatus.PENDING.value)
        
        # 确认
        confirm_result = self.executor.handle_confirm(signal.id, "trader_003")
        self.assertTrue(confirm_result)
        
        # 验证：最终执行
        signal_data = self.db.get_signal(signal.id)
        self.assertEqual(signal_data['status'], SignalStatus.EXECUTED.value)
    
    def test_reject_by_risk_control(self):
        """测试风控拒绝流程"""
        signal = Signal(
            id="integration_004",
            code="HK.00700",
            action=SignalAction.BUY,
            price=350.0,
            quantity=100
        )
        
        # 提交
        self.executor.submit_signal(signal)
        
        # 风控拒绝
        reject_result = self.executor.handle_reject(signal.id, "置信度过低")
        self.assertTrue(reject_result)
        
        # 验证状态
        signal_data = self.db.get_signal(signal.id)
        self.assertEqual(signal_data['status'], SignalStatus.REJECTED.value)
        
        # 验证：没有交易记录
        trades = self.db.get_open_trades()
        self.assertEqual(len(trades), 0)
    
    def test_multiple_signals_parallel(self):
        """测试多个信号并行处理"""
        signals = [
            Signal(id=f"multi_{i}", code=f"HK.0070{i}", action=SignalAction.BUY, price=350.0, quantity=100)
            for i in range(3)
        ]
        
        # 提交所有信号
        for signal in signals:
            result = self.executor.submit_signal(signal)
            self.assertTrue(result)
        
        # 验证：所有信号都是 pending
        for signal in signals:
            signal_data = self.db.get_signal(signal.id)
            self.assertEqual(signal_data['status'], SignalStatus.PENDING.value)
        
        # 确认第一个
        self.executor.handle_confirm(signals[0].id, "trader")
        
        # 忽略第二个
        self.executor.handle_ignore(signals[1].id, "trader")
        
        # 第三个保持 pending
        
        # 验证最终状态
        self.assertEqual(
            self.db.get_signal(signals[0].id)['status'],
            SignalStatus.EXECUTED.value
        )
        self.assertEqual(
            self.db.get_signal(signals[1].id)['status'],
            SignalStatus.IGNORED.value
        )
        self.assertEqual(
            self.db.get_signal(signals[2].id)['status'],
            SignalStatus.PENDING.value
        )
        
        # 验证：只有 1 个交易记录
        trades = self.db.get_open_trades()
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]['signal_id'], signals[0].id)
    
    def test_state_machine_transitions(self):
        """测试状态机流转"""
        signal = Signal(
            id="state_machine_001",
            code="HK.00700",
            action=SignalAction.BUY,
            price=350.0,
            quantity=100
        )
        
        # 初始状态：pending
        self.executor.submit_signal(signal)
        self.assertEqual(
            self.db.get_signal(signal.id)['status'],
            SignalStatus.PENDING.value
        )
        
        # pending -> confirmed -> executed
        self.executor.handle_confirm(signal.id, "trader")
        self.assertEqual(
            self.db.get_signal(signal.id)['status'],
            SignalStatus.EXECUTED.value
        )
        
        # 验证：不能再次确认
        signal2 = Signal(id="state_machine_002", code="HK.00700", action=SignalAction.BUY, price=350.0, quantity=100)
        self.executor.submit_signal(signal2)
        self.executor.handle_confirm(signal2.id, "trader")
        
        # 尝试再次确认应该失败
        result = self.executor.handle_confirm(signal2.id, "another_trader")
        self.assertFalse(result)
    
    def test_database_integrity(self):
        """测试数据库完整性"""
        signal = Signal(
            id="integrity_001",
            code="HK.00700",
            name="腾讯控股",
            action=SignalAction.BUY,
            price=350.0,
            quantity=100,
            reason="测试",
            strategy="测试策略"
        )
        
        # 提交并确认
        self.executor.submit_signal(signal)
        self.executor.handle_confirm(signal.id, "trader")
        
        # 验证信号数据完整性
        signal_data = self.db.get_signal(signal.id)
        self.assertEqual(signal_data['code'], signal.code)
        self.assertEqual(signal_data['name'], signal.name)
        self.assertEqual(signal_data['action'], signal.action.value)
        self.assertEqual(signal_data['price'], signal.price)
        self.assertEqual(signal_data['quantity'], signal.quantity)
        self.assertEqual(signal_data['strategy'], signal.strategy)
        self.assertEqual(signal_data['reason'], signal.reason)
        
        # 验证交易数据完整性
        trades = self.db.get_open_trades()
        self.assertEqual(len(trades), 1)
        trade = trades[0]
        self.assertEqual(trade['signal_id'], signal.id)
        self.assertEqual(trade['code'], signal.code)
        self.assertEqual(trade['action'], signal.action.value)
        self.assertEqual(trade['entry_price'], signal.price)
        self.assertEqual(trade['quantity'], signal.quantity)
        self.assertEqual(trade['status'], 'open')


if __name__ == '__main__':
    unittest.main(verbosity=2)
