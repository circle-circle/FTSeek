"""
PostTradeRisk 单元测试 - 交易后风控模块

测试覆盖:
- 日终盈亏统计
- 连续亏损熔断
- 策略表现评估
- 周报/月报生成
"""
import pytest
import time
from datetime import datetime, timedelta
from unittest.mock import Mock
import sys
import os

# 添加项目路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from app.v4.risk.post_trade import (
    PostTradeRisk,
    TradeRecord,
    DailyStats,
    PerformanceTracker
)


class TestTradeRecord:
    """交易记录测试"""
    
    def test_trade_record_creation(self):
        """测试交易记录创建"""
        trade = TradeRecord(
            code="HK.00700",
            direction="BUY",
            entry_price=100.0,
            exit_price=105.0,
            quantity=100,
            pnl=500.0,
            pnl_percent=0.05,
            strategy="price_break"
        )
        
        assert trade.code == "HK.00700"
        assert trade.direction == "BUY"
        assert trade.pnl == 500.0
        assert trade.pnl_percent == 0.05
        assert trade.is_win == True
    
    def test_trade_record_loss(self):
        """测试亏损交易记录"""
        trade = TradeRecord(
            code="HK.00700",
            direction="BUY",
            entry_price=100.0,
            exit_price=95.0,
            quantity=100,
            pnl=-500.0,
            pnl_percent=-0.05,
            strategy="price_break"
        )
        
        assert trade.is_win == False
        assert trade.pnl == -500.0


class TestDailyStats:
    """日终统计测试"""
    
    def test_daily_stats_calculation(self):
        """测试日终统计计算"""
        trades = [
            TradeRecord("HK.00700", "BUY", 100, 105, 100, 500, 0.05, "price_break"),
            TradeRecord("HK.09988", "BUY", 80, 82, 200, 400, 0.025, "macd"),
            TradeRecord("HK.00700", "SELL", 105, 103, 100, 200, 0.019, "rsi"),
        ]
        
        stats = DailyStats.calculate(trades)
        
        assert stats.total_trades == 3
        assert stats.winning_trades == 3
        assert stats.losing_trades == 0
        assert stats.win_rate == 1.0
        assert stats.net_pnl == 1100.0
        assert stats.gross_profit == 1100.0
        assert stats.gross_loss == 0.0
    
    def test_daily_stats_with_losses(self):
        """测试包含亏损的日终统计"""
        trades = [
            TradeRecord("HK.00700", "BUY", 100, 105, 100, 500, 0.05, "price_break"),
            TradeRecord("HK.09988", "BUY", 80, 75, 200, -1000, -0.0625, "macd"),
        ]
        
        stats = DailyStats.calculate(trades)
        
        assert stats.total_trades == 2
        assert stats.winning_trades == 1
        assert stats.losing_trades == 1
        assert stats.win_rate == 0.5
        assert stats.net_pnl == -500.0
        assert stats.gross_profit == 500.0
        assert stats.gross_loss == 1000.0
    
    def test_daily_stats_empty(self):
        """测试无交易的日终统计"""
        stats = DailyStats.calculate([])
        
        assert stats.total_trades == 0
        assert stats.win_rate == 0.0
        assert stats.net_pnl == 0.0


class TestPostTradeRisk:
    """PostTradeRisk 核心功能测试"""
    
    def test_consecutive_loss_tracking(self):
        """测试连续亏损追踪"""
        risk = PostTradeRisk(consecutive_loss_limit=3)
        
        # 记录连续亏损
        risk.record_loss(100)
        risk.record_loss(100)
        risk.record_loss(100)
        
        assert risk.consecutive_losses == 3
        assert risk.should_circuit_break() == True
    
    def test_consecutive_loss_reset_on_win(self):
        """测试盈利后连续亏损重置"""
        risk = PostTradeRisk(consecutive_loss_limit=3)
        
        risk.record_loss(100)
        risk.record_loss(100)
        risk.record_win(200)  # 盈利重置
        risk.record_loss(100)
        
        assert risk.consecutive_losses == 1
        assert risk.should_circuit_break() == False
    
    def test_daily_loss_limit(self):
        """测试日亏损限额"""
        risk = PostTradeRisk(daily_loss_limit=1000)
        
        # 记录亏损
        risk.record_loss(400)
        risk.record_loss(400)
        risk.record_loss(300)  # 超过限额
        
        assert risk.should_stop_trading() == True
    
    def test_daily_loss_limit_not_reached(self):
        """测试日亏损未达限额"""
        risk = PostTradeRisk(daily_loss_limit=1000)
        
        risk.record_loss(300)
        risk.record_loss(300)
        risk.record_loss(300)
        
        assert risk.should_stop_trading() == False
    
    def test_cooling_off_period(self):
        """测试冷静期"""
        risk = PostTradeRisk(consecutive_loss_limit=3, cooling_off_minutes=60)
        
        # 触发熔断
        for _ in range(3):
            risk.record_loss(100)
        
        assert risk.should_circuit_break() == True
        assert risk.cooling_off_remaining_minutes > 0
    
    def test_trade_recording(self):
        """测试交易记录"""
        risk = PostTradeRisk()
        
        trade = TradeRecord(
            code="HK.00700",
            direction="BUY",
            entry_price=100.0,
            exit_price=105.0,
            quantity=100,
            pnl=500.0,
            pnl_percent=0.05,
            strategy="price_break"
        )
        
        risk.record_trade(trade)
        
        assert len(risk.trades) == 1
        assert risk.trades[0].code == "HK.00700"
        assert risk.trades[0].pnl == 500.0
    
    def test_daily_stats_generation(self):
        """测试日终统计生成"""
        risk = PostTradeRisk()
        
        # 添加交易
        risk.record_trade(TradeRecord(
            "HK.00700", "BUY", 100, 105, 100, 500, 0.05, "price_break"
        ))
        risk.record_trade(TradeRecord(
            "HK.09988", "BUY", 80, 78, 200, -400, -0.025, "macd"
        ))
        
        stats = risk.generate_daily_stats()
        
        assert stats.total_trades == 2
        assert stats.net_pnl == 100.0
    
    def test_strategy_performance(self):
        """测试策略表现评估"""
        risk = PostTradeRisk()
        
        # 添加不同策略的交易
        risk.record_trade(TradeRecord(
            "HK.00700", "BUY", 100, 105, 100, 500, 0.05, "price_break"
        ))
        risk.record_trade(TradeRecord(
            "HK.09988", "BUY", 80, 82, 200, 400, 0.025, "price_break"
        ))
        risk.record_trade(TradeRecord(
            "HK.00700", "BUY", 100, 95, 100, -500, -0.05, "macd"
        ))
        
        perf = risk.get_strategy_performance()
        
        assert "price_break" in perf
        assert "macd" in perf
        assert perf["price_break"]["total_pnl"] == 900.0
        assert perf["macd"]["total_pnl"] == -500.0
    
    def test_reset_daily_stats(self):
        """测试日终统计重置"""
        risk = PostTradeRisk()
        
        risk.record_trade(TradeRecord(
            "HK.00700", "BUY", 100, 105, 100, 500, 0.05, "price_break"
        ))
        
        risk.reset_daily_stats()
        
        # reset_daily_stats 只重置日终统计，不重置总交易记录
        assert len(risk.daily_trades) == 0
        assert risk.daily_pnl == 0.0
        assert risk.consecutive_losses == 0
    
    def test_weekly_report_generation(self):
        """测试周报生成"""
        risk = PostTradeRisk()
        
        # 模拟一周交易
        for i in range(5):
            risk.record_trade(TradeRecord(
                "HK.00700", "BUY", 100, 100 + i, 100, i, i/100, "price_break"
            ))
        
        report = risk.generate_weekly_report()
        
        assert "total_trades" in report
        assert "net_pnl" in report
        assert "win_rate" in report
        assert report["total_trades"] == 5


class TestPerformanceTracker:
    """性能追踪器测试"""
    
    def test_trade_addition(self):
        """测试交易添加"""
        tracker = PerformanceTracker()
        
        trade = TradeRecord(
            "HK.00700", "BUY", 100, 105, 100, 500, 0.05, "price_break"
        )
        tracker.add_trade(trade)
        
        assert tracker.total_trades == 1
        assert tracker.total_pnl == 500.0
    
    def test_win_rate_calculation(self):
        """测试胜率计算"""
        tracker = PerformanceTracker()
        
        tracker.add_trade(TradeRecord("HK.00700", "BUY", 100, 105, 100, 500, 0.05, "price_break"))
        tracker.add_trade(TradeRecord("HK.00700", "BUY", 100, 95, 100, -500, -0.05, "price_break"))
        tracker.add_trade(TradeRecord("HK.00700", "BUY", 100, 110, 100, 1000, 0.10, "price_break"))
        
        assert tracker.total_trades == 3
        assert tracker.winning_trades == 2
        assert tracker.losing_trades == 1
        assert abs(tracker.win_rate - 0.667) < 0.01
    
    def test_profit_factor(self):
        """测试盈利因子计算"""
        tracker = PerformanceTracker()
        
        tracker.add_trade(TradeRecord("HK.00700", "BUY", 100, 105, 100, 500, 0.05, "price_break"))
        tracker.add_trade(TradeRecord("HK.00700", "BUY", 100, 110, 100, 1000, 0.10, "price_break"))
        tracker.add_trade(TradeRecord("HK.00700", "BUY", 100, 95, 100, -500, -0.05, "price_break"))
        
        # 盈利因子 = 总盈利 / 总亏损
        assert tracker.gross_profit == 1500.0
        assert tracker.gross_loss == 500.0
        assert tracker.profit_factor == 3.0
    
    def test_max_drawdown(self):
        """测试最大回撤计算"""
        tracker = PerformanceTracker(initial_capital=10000)
        
        # 模拟资金曲线：10000 -> 11000 -> 10500 -> 12000 -> 11500
        tracker.update_capital(11000)
        tracker.update_capital(10500)
        tracker.update_capital(12000)
        tracker.update_capital(11500)
        
        # 最大回撤发生在 12000 -> 11500
        assert tracker.peak_capital == 12000
        assert tracker.current_capital == 11500
        assert abs(tracker.current_drawdown - (12000 - 11500) / 12000) < 0.001
    
    def test_sharpe_ratio(self):
        """测试夏普比率计算"""
        tracker = PerformanceTracker()
        
        # 通过交易添加收益率
        for i, r in enumerate([0.02, -0.01, 0.03, 0.01, -0.02, 0.04, 0.01]):
            # 模拟交易产生收益
            pnl = r * 10000
            tracker.add_trade(TradeRecord(
                "HK.00700", "BUY", 100, 100 + pnl/100, 100, pnl, r, "test"
            ))
        
        sharpe = tracker.calculate_sharpe_ratio()
        assert sharpe is not None
        assert isinstance(sharpe, float)
    
    def test_strategy_breakdown(self):
        """测试策略细分统计"""
        tracker = PerformanceTracker()
        
        tracker.add_trade(TradeRecord("HK.00700", "BUY", 100, 105, 100, 500, 0.05, "price_break"))
        tracker.add_trade(TradeRecord("HK.00700", "BUY", 100, 105, 100, 500, 0.05, "price_break"))
        tracker.add_trade(TradeRecord("HK.00700", "BUY", 100, 95, 100, -500, -0.05, "macd"))
        
        breakdown = tracker.get_strategy_breakdown()
        
        assert "price_break" in breakdown
        assert "macd" in breakdown
        assert breakdown["price_break"]["trades"] == 2
        assert breakdown["macd"]["trades"] == 1
    
    def test_monthly_performance(self):
        """测试月度表现"""
        tracker = PerformanceTracker()
        
        # 模拟一个月交易
        for i in range(20):
            pnl = 100 if i % 2 == 0 else -50
            tracker.add_trade(TradeRecord(
                "HK.00700", "BUY", 100, 100 + pnl/100, 100, pnl, pnl/100, "price_break"
            ))
        
        monthly = tracker.get_monthly_performance()
        
        assert "total_trades" in monthly
        assert "net_pnl" in monthly
        assert monthly["total_trades"] == 20


class TestRiskMetrics:
    """风险指标测试"""
    
    def test_average_win_loss_ratio(self):
        """测试平均盈亏比"""
        risk = PostTradeRisk()
        
        risk.record_trade(TradeRecord("HK.00700", "BUY", 100, 105, 100, 500, 0.05, "price_break"))
        risk.record_trade(TradeRecord("HK.00700", "BUY", 100, 110, 100, 1000, 0.10, "price_break"))
        risk.record_trade(TradeRecord("HK.00700", "BUY", 100, 95, 100, -500, -0.05, "price_break"))
        
        metrics = risk.get_risk_metrics()
        
        assert "avg_win" in metrics
        assert "avg_loss" in metrics
        assert "win_loss_ratio" in metrics
    
    def test_largest_win_loss(self):
        """测试最大盈利/亏损"""
        risk = PostTradeRisk()
        
        risk.record_trade(TradeRecord("HK.00700", "BUY", 100, 105, 100, 500, 0.05, "price_break"))
        risk.record_trade(TradeRecord("HK.00700", "BUY", 100, 110, 100, 1000, 0.10, "price_break"))
        risk.record_trade(TradeRecord("HK.00700", "BUY", 100, 90, 100, -1000, -0.10, "price_break"))
        
        metrics = risk.get_risk_metrics()
        
        # metrics 返回的是字符串格式
        assert metrics["largest_win"] == "1000.00"
        assert metrics["largest_loss"] == "-1000.00"
    
    def test_average_holding_period(self):
        """测试平均持仓时间"""
        risk = PostTradeRisk()
        
        # 添加带持仓时间的交易
        trade1 = TradeRecord("HK.00700", "BUY", 100, 105, 100, 500, 0.05, "price_break")
        trade1.entry_time = time.time() - 3600  # 1 小时前
        trade1.exit_time = time.time()
        
        risk.record_trade(trade1)
        
        metrics = risk.get_risk_metrics()
        assert "avg_holding_period_minutes" in metrics


# 运行测试
if __name__ == "__main__":
    pytest.main([__file__, "-v"])
