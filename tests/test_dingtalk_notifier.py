"""
钉钉推送器单元测试
测试详细版推送模板：开仓推送、平仓推送
"""
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch, MagicMock
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from app.strategy.base import Signal, SignalAction
from app.trader.executor import TradeResult
from app.trader.position_manager import Position


@dataclass
class MockConsensusResult:
    """模拟共识结果"""
    code: str
    action: SignalAction
    price: float
    score: float
    threshold: float
    market_regime: str
    adx_value: Optional[float]
    agree_strategies: List[str]
    oppose_strategies: List[str]
    weight_details: Dict[str, float]
    reasons: List[str]
    votes: List
    # 添加任务要求的字段
    strategy_scores: Dict[str, float] = field(default_factory=dict)
    confidence: float = 0.85


class TestDingTalkNotifier(unittest.TestCase):
    """钉钉推送器测试"""

    def setUp(self):
        """测试前准备"""
        from app.notifier.dingtalk import DingTalkNotifier
        
        self.notifier = DingTalkNotifier(
            webhook_url="https://oapi.dingtalk.com/robot/send?access_token=test",
            secret="test_secret"
        )
        
        # 模拟信号
        self.signal = Signal(
            id="test_signal_001",
            code="HK.00700",
            name="腾讯控股",
            action=SignalAction.BUY,
            price=350.50,
            quantity=100,
            reason="RSI 超卖 + 支撑位反弹",
            strategy="rsi_rebound",
            extra={"consensus": True}
        )
        
        # 模拟交易结果
        self.trade_result = TradeResult(
            success=True,
            order_id="ORDER_20260313_001",
            position_id=1,
            trade_type="OPEN",
            pnl=None,
            message="开仓成功"
        )
        
        # 模拟持仓
        self.position = Position(
            id=1,
            code="HK.00700",
            action="BUY",
            quantity=100,
            entry_price=350.50,
            entry_time=(datetime.now() - timedelta(hours=2, minutes=30)).strftime('%Y-%m-%d %H:%M:%S'),
            current_price=355.00,
            unrealized_pnl=450.00,
            status="OPEN"
        )
        
        # 模拟共识结果
        self.consensus_result = MockConsensusResult(
            code="HK.00700",
            action=SignalAction.BUY,
            price=350.50,
            score=2.3,
            threshold=1.5,
            market_regime="trending",
            adx_value=28.5,
            agree_strategies=["rsi", "supertrend"],
            oppose_strategies=["macd"],
            weight_details={"rsi": 0.8, "supertrend": 1.0, "macd": 0.8},
            reasons=["RSI 超卖", "趋势突破"],
            votes=[],
            strategy_scores={
                "rsi": 0.8,
                "supertrend": 1.5,
                "macd": -0.6
            },
            confidence=0.85
        )

    def test_format_strategies(self):
        """测试策略格式化"""
        result = self.notifier._format_strategies(self.consensus_result)
        
        # 验证格式
        self.assertIn("✅ rsi: +0.8", result)
        self.assertIn("✅ supertrend: +1.5", result)
        self.assertIn("❌ macd: -0.6", result)
        
        # 验证行数
        lines = result.split("\n")
        self.assertEqual(len(lines), 3)

    def test_format_risk_checks(self):
        """测试风控检查格式化"""
        result = self.notifier._format_risk_checks()
        
        # 验证包含所有检查项
        self.assertIn("✅ 仓位", result)
        self.assertIn("✅ 日交易", result)
        self.assertIn("✅ 回撤", result)

    def test_calculate_hold_time_hours_and_minutes(self):
        """测试持仓时长计算（小时 + 分钟）"""
        entry_time = datetime.now() - timedelta(hours=2, minutes=30)
        exit_time = datetime.now()
        
        result = self.notifier._calculate_hold_time(entry_time, exit_time)
        
        self.assertIn("小时", result)
        self.assertIn("分钟", result)
        self.assertIn("2", result)  # 2 小时
        # 验证包含 29 或 30 分钟（允许秒级误差）
        self.assertTrue("29" in result or "30" in result)

    def test_calculate_hold_time_minutes_only(self):
        """测试持仓时长计算（仅分钟）"""
        entry_time = datetime.now() - timedelta(minutes=45)
        exit_time = datetime.now()
        
        result = self.notifier._calculate_hold_time(entry_time, exit_time)
        
        self.assertNotIn("小时", result)
        self.assertIn("分钟", result)
        # 验证包含 44 或 45 分钟（允许秒级误差）
        self.assertTrue("44" in result or "45" in result)

    def test_calculate_hold_time_string_input(self):
        """测试持仓时长计算（字符串输入）"""
        entry_time = (datetime.now() - timedelta(hours=1, minutes=15)).strftime('%Y-%m-%d %H:%M:%S')
        exit_time = datetime.now()
        
        result = self.notifier._calculate_hold_time(entry_time, exit_time)
        
        self.assertIn("小时", result)
        self.assertIn("分钟", result)

    def test_get_stock_name_hk(self):
        """测试港股名称获取"""
        result = self.notifier._get_stock_name("HK.00700")
        self.assertEqual(result, "00700")

    def test_get_stock_name_us(self):
        """测试美股名称获取"""
        result = self.notifier._get_stock_name("US.AAPL")
        self.assertEqual(result, "AAPL")

    @patch('app.notifier.dingtalk.requests.post')
    def test_send_open_position_message(self, mock_post):
        """测试开仓详细推送"""
        # 配置 mock 响应
        mock_response = Mock()
        mock_response.json.return_value = {"errcode": 0}
        mock_post.return_value = mock_response
        
        # 调用方法
        result = self.notifier.send_open_position_message(
            self.signal,
            self.trade_result,
            self.position,
            self.consensus_result
        )
        
        # 验证返回
        self.assertTrue(result)
        
        # 验证 POST 调用
        self.assertEqual(mock_post.call_count, 1)
        
        # 获取发送的消息内容
        call_args = mock_post.call_args
        payload = call_args[1]['json']
        
        # 验证消息类型
        self.assertEqual(payload['msgtype'], 'markdown')
        
        # 验证消息内容
        text = payload['markdown']['text']
        
        # 验证开仓推送关键信息
        self.assertIn("✅ HK.00700 00700 买入成交", text)
        self.assertIn("订单 ID: ORDER_20260313_001", text)
        self.assertIn("数量：100 股", text)
        self.assertIn("成交价：HK$ 350.50", text)
        self.assertIn("总金额：HK$ 35,050.00", text)
        self.assertIn("策略信号:", text)
        self.assertIn("+2.30", text)  # 加权分
        self.assertIn("85%", text)  # 置信度
        self.assertIn("trending", text)  # 市场状态
        self.assertIn("风控检查:", text)
        
        # 验证 Markdown 分隔符
        self.assertIn("━━━━━━━━━━━━━━━━━━━━", text)

    @patch('app.notifier.dingtalk.requests.post')
    def test_send_close_position_message_profit(self, mock_post):
        """测试平仓详细推送（盈利）"""
        # 配置 mock 响应
        mock_response = Mock()
        mock_response.json.return_value = {"errcode": 0}
        mock_post.return_value = mock_response
        
        # 修改交易结果为平仓（TradeResult 没有 exit_price 字段）
        close_trade_result = TradeResult(
            success=True,
            order_id="ORDER_20260313_002",
            position_id=1,
            trade_type="CLOSE",
            pnl=450.00,
            message="平仓成功"
        )
        
        # 调用方法
        result = self.notifier.send_close_position_message(
            self.signal,
            close_trade_result,
            self.position,
            self.consensus_result
        )
        
        # 验证返回
        self.assertTrue(result)
        
        # 验证 POST 调用
        self.assertEqual(mock_post.call_count, 1)
        
        # 获取发送的消息内容
        call_args = mock_post.call_args
        payload = call_args[1]['json']
        
        # 验证消息类型
        self.assertEqual(payload['msgtype'], 'markdown')
        
        # 验证消息内容
        text = payload['markdown']['text']
        
        # 验证平仓推送关键信息
        self.assertIn("📊 HK.00700", text)  # 盈利使用📊
        self.assertIn("平仓盈亏", text)
        self.assertIn("开仓：100 股 @ HK$ 350.50", text)
        self.assertIn("平仓：100 股 @ HK$", text)
        self.assertIn("💰 盈亏：HK$ +450.00", text)  # 盈利使用💰
        self.assertIn("持仓时长：", text)
        self.assertIn("策略：反向信号触发平仓", text)
        self.assertIn("原信号：BUY → 平仓信号：BUY", text)
        
        # 验证 Markdown 分隔符
        self.assertIn("━━━━━━━━━━━━━━━━━━━━", text)

    @patch('app.notifier.dingtalk.requests.post')
    def test_send_close_position_message_loss(self, mock_post):
        """测试平仓详细推送（亏损）"""
        # 配置 mock 响应
        mock_response = Mock()
        mock_response.json.return_value = {"errcode": 0}
        mock_post.return_value = mock_response
        
        # 修改交易结果为亏损平仓（TradeResult 没有 exit_price 字段）
        close_trade_result = TradeResult(
            success=True,
            order_id="ORDER_20260313_003",
            position_id=1,
            trade_type="CLOSE",
            pnl=-500.00,
            message="平仓止损"
        )
        
        # 调用方法
        result = self.notifier.send_close_position_message(
            self.signal,
            close_trade_result,
            self.position,
            self.consensus_result
        )
        
        # 验证返回
        self.assertTrue(result)
        
        # 获取发送的消息内容
        call_args = mock_post.call_args
        payload = call_args[1]['json']
        text = payload['markdown']['text']
        
        # 验证亏损图标
        self.assertIn("⚠️ HK.00700", text)  # 亏损使用⚠️
        self.assertIn("平仓盈亏", text)
        self.assertIn("💸 盈亏：HK$ -500.00", text)  # 亏损使用💸

    @patch('app.notifier.dingtalk.requests.post')
    def test_send_open_position_message_rate_limit(self, mock_post):
        """测试开仓推送限流"""
        # 模拟限流
        with patch.object(self.notifier, '_can_send', return_value=False):
            result = self.notifier.send_open_position_message(
                self.signal,
                self.trade_result,
                self.position,
                self.consensus_result
            )
            
            # 验证被限流
            self.assertFalse(result)
            mock_post.assert_not_called()

    @patch('app.notifier.dingtalk.requests.post')
    def test_send_close_position_message_rate_limit(self, mock_post):
        """测试平仓推送限流"""
        # 模拟限流
        with patch.object(self.notifier, '_can_send', return_value=False):
            close_trade_result = TradeResult(
                success=True,
                order_id="ORDER_003",
                position_id=1,
                trade_type="CLOSE",
                pnl=100.00
            )
            
            result = self.notifier.send_close_position_message(
                self.signal,
                close_trade_result,
                self.position,
                self.consensus_result
            )
            
            # 验证被限流
            self.assertFalse(result)
            mock_post.assert_not_called()

    @patch('app.notifier.dingtalk.requests.post')
    def test_send_markdown(self, mock_post):
        """测试 Markdown 消息发送"""
        # 配置 mock 响应
        mock_response = Mock()
        mock_response.json.return_value = {"errcode": 0}
        mock_post.return_value = mock_response
        
        # 调用方法
        result = self.notifier._send_markdown("测试消息")
        
        # 验证返回
        self.assertTrue(result)
        
        # 验证消息格式
        call_args = mock_post.call_args
        payload = call_args[1]['json']
        
        self.assertEqual(payload['msgtype'], 'markdown')
        self.assertEqual(payload['markdown']['text'], "测试消息")


class TestDingTalkNotifierSellSignal(unittest.TestCase):
    """测试卖出信号推送"""

    def setUp(self):
        """测试前准备"""
        from app.notifier.dingtalk import DingTalkNotifier
        
        self.notifier = DingTalkNotifier(
            webhook_url="https://oapi.dingtalk.com/robot/send?access_token=test",
            secret="test_secret"
        )
        
        # 模拟卖出信号
        self.signal = Signal(
            id="test_signal_002",
            code="HK.09988",
            name="阿里巴巴",
            action=SignalAction.SELL,
            price=78.50,
            quantity=200,
            reason="MACD 死叉 + 阻力位",
            strategy="macd",
            extra={"consensus": True}
        )
        
        # 模拟交易结果
        self.trade_result = TradeResult(
            success=True,
            order_id="ORDER_20260313_004",
            position_id=2,
            trade_type="OPEN",
            pnl=None,
            message="卖出开仓成功"
        )
        
        # 模拟持仓
        self.position = Position(
            id=2,
            code="HK.09988",
            action="SELL",
            quantity=200,
            entry_price=78.50,
            entry_time=(datetime.now() - timedelta(hours=5)).strftime('%Y-%m-%d %H:%M:%S'),
            current_price=77.00,
            unrealized_pnl=300.00,
            status="OPEN"
        )
        
        # 模拟共识结果
        self.consensus_result = MockConsensusResult(
            code="HK.09988",
            action=SignalAction.SELL,
            price=78.50,
            score=-1.8,
            threshold=1.5,
            market_regime="ranging",
            adx_value=18.2,
            agree_strategies=["macd", "bollinger"],
            oppose_strategies=["rsi"],
            weight_details={"macd": 0.8, "bollinger": 0.8, "rsi": 0.8},
            reasons=["MACD 死叉", "布林带上轨阻力"],
            votes=[],
            strategy_scores={
                "macd": -0.9,
                "bollinger": -0.9,
                "rsi": 0.5
            },
            confidence=0.78
        )

    @patch('app.notifier.dingtalk.requests.post')
    def test_send_open_position_message_sell(self, mock_post):
        """测试卖出开仓推送"""
        # 配置 mock 响应
        mock_response = Mock()
        mock_response.json.return_value = {"errcode": 0}
        mock_post.return_value = mock_response
        
        # 调用方法
        result = self.notifier.send_open_position_message(
            self.signal,
            self.trade_result,
            self.position,
            self.consensus_result
        )
        
        # 验证返回
        self.assertTrue(result)
        
        # 获取发送的消息内容
        call_args = mock_post.call_args
        payload = call_args[1]['json']
        text = payload['markdown']['text']
        
        # 验证卖出推送内容
        self.assertIn("✅ HK.09988 09988 卖出成交", text)
        self.assertIn("数量：200 股", text)
        self.assertIn("成交价：HK$ 78.50", text)
        self.assertIn("总金额：HK$ 15,700.00", text)
        self.assertIn("-1.80", text)  # 加权分
        self.assertIn("78%", text)  # 置信度
        self.assertIn("ranging", text)  # 市场状态


if __name__ == '__main__':
    unittest.main()
