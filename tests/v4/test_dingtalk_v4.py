"""
DingTalkNotifier V4 单元测试 - 钉钉推送格式化模块

测试覆盖:
- 盘前观察列表推送 (09:00)
- 交易信号推送 (实时)
- 风险监控警报 (实时)
- 收盘统计报告 (16:30)
"""
import pytest
import time
from unittest.mock import Mock
import sys
import os

# 添加项目路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from app.v4.notifier.dingtalk_v4 import (
    DingTalkNotifierV4,
    PushType,
    AlertLevel,
    WatchlistItem,
    SignalPush,
    AlertPush,
    DailyReport
)


class TestPushType:
    """推送类型枚举测试"""
    
    def test_push_type_enum(self):
        """测试推送类型枚举"""
        assert PushType.PREMARKET_WATCHLIST == "PREMARKET_WATCHLIST"
        assert PushType.TRADE_SIGNAL == "TRADE_SIGNAL"
        assert PushType.RISK_ALERT == "RISK_ALERT"
        assert PushType.DAILY_REPORT == "DAILY_REPORT"


class TestAlertLevel:
    """警报级别枚举测试"""
    
    def test_alert_level_enum(self):
        """测试警报级别枚举"""
        assert AlertLevel.INFO == "INFO"
        assert AlertLevel.WARNING == "WARNING"
        assert AlertLevel.CRITICAL == "CRITICAL"


class TestWatchlistItem:
    """WatchlistItem 数据类测试"""
    
    def test_watchlist_item_creation(self):
        """测试观察列表项创建"""
        item = WatchlistItem(
            code="HK.00700",
            name="腾讯控股",
            priority=1,
            reason="盘前放量 3 倍",
            watch_price=100.0
        )
        
        assert item.code == "HK.00700"
        assert item.priority == 1
        assert item.reason == "盘前放量 3 倍"
    
    def test_watchlist_to_dict(self):
        """测试观察列表项转字典"""
        item = WatchlistItem(
            code="HK.00700",
            name="腾讯控股",
            priority=1,
            reason="突破形态"
        )
        
        item_dict = item.to_dict()
        
        assert item_dict["code"] == "HK.00700"
        assert item_dict["priority"] == 1


class TestSignalPush:
    """SignalPush 数据类测试"""
    
    def test_signal_push_creation(self):
        """测试信号推送创建"""
        push = SignalPush(
            code="HK.00700",
            action="BUY",
            entry_price=100.0,
            stop_loss=97.0,
            target_price=110.0,
            confidence=0.85,
            reason="突破关键阻力位"
        )
        
        assert push.code == "HK.00700"
        assert push.action == "BUY"
        assert push.confidence == 0.85
    
    def test_signal_push_to_dict(self):
        """测试信号推送转字典"""
        push = SignalPush(
            code="HK.00700",
            action="BUY",
            entry_price=100.0,
            stop_loss=97.0,
            target_price=110.0,
            confidence=0.85
        )
        
        push_dict = push.to_dict()
        
        assert push_dict["code"] == "HK.00700"
        assert push_dict["action"] == "BUY"


class TestAlertPush:
    """AlertPush 数据类测试"""
    
    def test_alert_push_creation(self):
        """测试警报推送创建"""
        alert = AlertPush(
            level=AlertLevel.WARNING,
            alert_type="STOP_LOSS",
            code="HK.00700",
            message="接近止损位"
        )
        
        assert alert.level == AlertLevel.WARNING
        assert alert.alert_type == "STOP_LOSS"
        assert alert.code == "HK.00700"
    
    def test_alert_push_to_dict(self):
        """测试警报推送转字典"""
        alert = AlertPush(
            level=AlertLevel.CRITICAL,
            alert_type="CIRCUIT_BREAK",
            code="HK.00700",
            message="触发连续亏损熔断"
        )
        
        alert_dict = alert.to_dict()
        
        assert alert_dict["level"] == "CRITICAL"
        assert alert_dict["alert_type"] == "CIRCUIT_BREAK"


class TestDailyReport:
    """DailyReport 数据类测试"""
    
    def test_daily_report_creation(self):
        """测试日报创建"""
        report = DailyReport(
            date="2026-03-08",
            total_trades=5,
            winning_trades=3,
            net_pnl=1500.0,
            win_rate=0.60
        )
        
        assert report.date == "2026-03-08"
        assert report.total_trades == 5
        assert report.net_pnl == 1500.0
    
    def test_daily_report_to_dict(self):
        """测试日报转字典"""
        report = DailyReport(
            date="2026-03-08",
            total_trades=5,
            winning_trades=3,
            net_pnl=1500.0,
            win_rate=0.60
        )
        
        report_dict = report.to_dict()
        
        assert report_dict["date"] == "2026-03-08"
        assert report_dict["total_trades"] == 5


class TestDingTalkNotifierV4:
    """DingTalkNotifierV4 核心功能测试"""
    
    def test_notifier_initialization(self):
        """测试通知器初始化"""
        notifier = DingTalkNotifierV4()
        
        assert notifier.webhook_url is None  # 需要配置
        assert notifier.secret is None
        assert notifier.enable_push is True
    
    def test_premarket_watchlist_format(self):
        """测试盘前观察列表格式"""
        notifier = DingTalkNotifierV4()
        
        watchlist = [
            WatchlistItem(code="HK.00700", name="腾讯", priority=1, reason="盘前放量"),
            WatchlistItem(code="HK.09988", name="阿里", priority=2, reason="突破形态"),
        ]
        
        output = notifier.format_premarket_watchlist(watchlist)
        
        assert "盘前观察" in output or "观察列表" in output
        assert "HK.00700" in output
        assert "HK.09988" in output
        assert "腾讯" in output or "阿里" in output
    
    def test_trade_signal_format(self):
        """测试交易信号格式"""
        notifier = DingTalkNotifierV4()
        
        signal = SignalPush(
            code="HK.00700",
            action="BUY",
            entry_price=100.0,
            stop_loss=97.0,
            target_price=110.0,
            confidence=0.85,
            reason="突破"
        )
        
        output = notifier.format_trade_signal(signal)
        
        assert "买入" in output or "BUY" in output
        assert "100" in output  # 入场价
        assert "97" in output   # 止损
        assert "110" in output  # 目标
    
    def test_risk_alert_format(self):
        """测试风险警报格式"""
        notifier = DingTalkNotifierV4()
        
        alert = AlertPush(
            level=AlertLevel.CRITICAL,
            alert_type="STOP_LOSS",
            code="HK.00700",
            message="触及止损"
        )
        
        output = notifier.format_risk_alert(alert)
        
        # 紧急警报应有明显标记
        assert any(icon in output for icon in ["🚨", "⚠️", "❗"]) or \
               "警告" in output or "警报" in output
    
    def test_daily_report_format(self):
        """测试日报格式"""
        notifier = DingTalkNotifierV4()
        
        report = DailyReport(
            date="2026-03-08",
            total_trades=5,
            winning_trades=3,
            net_pnl=1500.0,
            win_rate=0.60
        )
        
        output = notifier.format_daily_report(report)
        
        assert "日报" in output or "总结" in output
        assert "5" in output  # 交易数
        assert "60%" in output or "0.6" in output  # 胜率
        assert "1500" in output  # 盈亏
    
    def test_urgent_alert_emoji(self):
        """测试紧急警报 emoji"""
        notifier = DingTalkNotifierV4()
        
        # 紧急警报
        alert_critical = AlertPush(
            level=AlertLevel.CRITICAL,
            alert_type="CIRCUIT_BREAK",
            code="HK.00700",
            message="熔断"
        )
        
        output = notifier.format_risk_alert(alert_critical)
        
        # 紧急警报应有 emoji
        assert any(char in output for char in ["🚨", "⚠️", "❗", "🛑"])
    
    def test_signal_with_confidence(self):
        """测试带置信度的信号"""
        notifier = DingTalkNotifierV4()
        
        signal = SignalPush(
            code="HK.00700",
            action="BUY",
            entry_price=100.0,
            stop_loss=97.0,
            target_price=110.0,
            confidence=0.90
        )
        
        output = notifier.format_trade_signal(signal)
        
        # 应包含置信度
        assert "90%" in output or "0.9" in output or "高" in output
    
    def test_watchlist_priority_ordering(self):
        """测试观察列表优先级排序"""
        notifier = DingTalkNotifierV4()
        
        watchlist = [
            WatchlistItem(code="HK.00700", priority=3),
            WatchlistItem(code="HK.09988", priority=1),
            WatchlistItem(code="HK.09992", priority=2),
        ]
        
        output = notifier.format_premarket_watchlist(watchlist)
        
        # 优先级 1 的应在前面
        assert output.index("HK.09988") < output.index("HK.09992")
        assert output.index("HK.09992") < output.index("HK.00700")
    
    def test_report_with_profit_loss(self):
        """测试带盈亏的日报"""
        notifier = DingTalkNotifierV4()
        
        # 盈利日报
        report_profit = DailyReport(
            date="2026-03-08",
            total_trades=5,
            winning_trades=4,
            net_pnl=2000.0,
            win_rate=0.80
        )
        
        # 亏损日报
        report_loss = DailyReport(
            date="2026-03-08",
            total_trades=5,
            winning_trades=2,
            net_pnl=-1000.0,
            win_rate=0.40
        )
        
        output_profit = notifier.format_daily_report(report_profit)
        output_loss = notifier.format_daily_report(report_loss)
        
        # 盈利应有正号或绿色标记
        assert "+" in output_profit or "盈利" in output_profit
        
        # 亏损应有负号或红色标记
        assert "-" in output_loss or "亏损" in output_loss
    
    def test_message_length_check(self):
        """测试消息长度检查"""
        notifier = DingTalkNotifierV4(max_length=150)
        
        signal = SignalPush(
            code="HK.00700",
            action="BUY",
            entry_price=100.0,
            stop_loss=97.0,
            target_price=110.0,
            confidence=0.85,
            reason="这是一个非常长的理由用来测试消息长度限制功能"
        )
        
        output = notifier.format_trade_signal(signal)
        
        assert len(output) <= 150
    
    def test_multiple_alerts_aggregation(self):
        """测试多警报聚合"""
        notifier = DingTalkNotifierV4()
        
        alerts = [
            AlertPush(level=AlertLevel.WARNING, alert_type="STOP_LOSS", code="HK.00700", message="接近止损"),
            AlertPush(level=AlertLevel.INFO, alert_type="PROFIT_TARGET", code="HK.09988", message="达到目标"),
        ]
        
        output = notifier.format_multiple_alerts(alerts)
        
        assert "HK.00700" in output
        assert "HK.09988" in output
        assert len(output.split("\n")) >= 2
    
    def test_custom_template(self):
        """测试自定义模板"""
        notifier = DingTalkNotifierV4()
        
        template = "【自定义】{code} {action} @ {entry_price}"
        
        signal = SignalPush(
            code="HK.00700",
            action="BUY",
            entry_price=100.0,
            stop_loss=97.0,
            target_price=110.0,
            confidence=0.85
        )
        
        output = notifier.format_with_template(signal, template)
        
        assert "【自定义】" in output
        assert "HK.00700" in output
        assert "BUY" in output
        assert "100" in output


class TestDingTalkIntegration:
    """集成测试"""
    
    def test_webhook_configuration(self):
        """测试 webhook 配置"""
        notifier = DingTalkNotifierV4(
            webhook_url="https://oapi.dingtalk.com/robot/send?access_token=test",
            secret="SEC_test"
        )
        
        assert notifier.webhook_url is not None
        assert notifier.secret is not None
    
    def test_send_message_mock(self):
        """测试发送消息 (Mock)"""
        notifier = DingTalkNotifierV4(
            webhook_url="https://oapi.dingtalk.com/robot/send?access_token=test"
        )
        
        # Mock requests.post
        import requests
        original_post = requests.post
        
        class MockResponse:
            def json(self):
                return {"errcode": 0, "errmsg": "ok"}
        
        requests.post = Mock(return_value=MockResponse())
        
        result = notifier.send("测试消息")
        
        requests.post = original_post
        assert result.get("success") == True


# 运行测试
if __name__ == "__main__":
    pytest.main([__file__, "-v"])
