"""
SimpleBacktest 单元测试 - 简化版回测模块

测试覆盖:
- 加载历史信号
- 模拟执行结果
- 计算胜率/盈亏比/夏普比率
- 生成回测报告
"""
import pytest
import sys
import os

# 添加项目路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from app.v4.backtest.simple_backtest import (
    SimpleBacktest,
    BacktestResult,
    BacktestConfig,
    Trade
)


class TestTrade:
    """Trade 数据类测试"""
    
    def test_trade_creation(self):
        """测试交易创建"""
        trade = Trade(
            code="HK.00700",
            entry_price=100.0,
            exit_price=105.0,
            quantity=100,
            pnl=500.0
        )
        
        assert trade.code == "HK.00700"
        assert trade.pnl == 500.0
    
    def test_trade_return_calculation(self):
        """测试收益率计算"""
        trade = Trade(
            code="HK.00700",
            entry_price=100.0,
            exit_price=105.0,
            quantity=100,
            pnl=500.0
        )
        
        assert abs(trade.return_pct - 0.05) < 0.001


class TestBacktestResult:
    """BacktestResult 数据类测试"""
    
    def test_result_creation(self):
        """测试回测结果创建"""
        result = BacktestResult(
            total_trades=100,
            winning_trades=60,
            losing_trades=40,
            net_pnl=10000.0,
            win_rate=0.60,
            profit_factor=2.0,
            sharpe_ratio=1.5,
            max_drawdown=0.15,
            total_return=0.10
        )
        
        assert result.total_trades == 100
        assert result.win_rate == 0.60
        assert result.sharpe_ratio == 1.5
    
    def test_result_quality_rating(self):
        """测试质量评级"""
        # 优秀结果
        result_good = BacktestResult(
            total_trades=100,
            winning_trades=70,
            losing_trades=30,
            net_pnl=20000.0,
            win_rate=0.70,
            profit_factor=3.0,
            sharpe_ratio=2.0,
            max_drawdown=0.10,
            total_return=0.20
        )
        
        assert result_good.get_rating() in ["EXCELLENT", "GOOD"]
        
        # 差结果
        result_bad = BacktestResult(
            total_trades=100,
            winning_trades=30,
            losing_trades=70,
            net_pnl=-5000.0,
            win_rate=0.30,
            profit_factor=0.5,
            sharpe_ratio=-1.0,
            max_drawdown=0.40,
            total_return=-0.05
        )
        
        assert result_bad.get_rating() in ["POOR", "VERY_POOR"]


class TestBacktestConfig:
    """BacktestConfig 数据类测试"""
    
    def test_config_creation(self):
        """测试配置创建"""
        config = BacktestConfig(
            initial_capital=100000.0,
            commission_rate=0.0003,
            slippage_rate=0.001
        )
        
        assert config.initial_capital == 100000.0
        assert config.commission_rate == 0.0003


class TestSimpleBacktest:
    """SimpleBacktest 核心功能测试"""
    
    def test_backtest_initialization(self):
        """测试回测初始化"""
        backtest = SimpleBacktest()
        
        assert backtest.initial_capital == 100000.0
        assert len(backtest.trades) == 0
    
    def test_add_signal(self):
        """测试添加信号"""
        backtest = SimpleBacktest()
        
        signal = {
            "code": "HK.00700",
            "action": "BUY",
            "entry_price": 100.0,
            "exit_price": 105.0,
            "quantity": 100
        }
        
        backtest.add_signal(signal)
        
        assert len(backtest.signals) == 1
        assert backtest.signals[0]["code"] == "HK.00700"
    
    def test_run_backtest(self):
        """测试运行回测"""
        backtest = SimpleBacktest()
        
        # 添加信号
        for i in range(10):
            backtest.add_signal({
                "code": "HK.00700",
                "action": "BUY",
                "entry_price": 100.0,
                "exit_price": 100.0 + i,
                "quantity": 100
            })
        
        result = backtest.run()
        
        assert result.total_trades == 10
        assert result.net_pnl > 0 or result.net_pnl < 0  # 有盈亏
    
    def test_win_rate_calculation(self):
        """测试胜率计算"""
        backtest = SimpleBacktest()
        
        # 6 胜 4 负
        for i in range(6):
            backtest.add_signal({
                "code": "HK.00700",
                "action": "BUY",
                "entry_price": 100.0,
                "exit_price": 110.0,
                "quantity": 100
            })
        
        for i in range(4):
            backtest.add_signal({
                "code": "HK.00700",
                "action": "BUY",
                "entry_price": 100.0,
                "exit_price": 90.0,
                "quantity": 100
            })
        
        result = backtest.run()
        
        assert result.win_rate == 0.60
        assert result.winning_trades == 6
        assert result.losing_trades == 4
    
    def test_profit_factor_calculation(self):
        """测试盈利因子计算"""
        backtest = SimpleBacktest()
        
        # 盈利交易
        for i in range(5):
            backtest.add_signal({
                "code": "HK.00700",
                "action": "BUY",
                "entry_price": 100.0,
                "exit_price": 120.0,
                "quantity": 100
            })
        
        # 亏损交易
        for i in range(5):
            backtest.add_signal({
                "code": "HK.00700",
                "action": "BUY",
                "entry_price": 100.0,
                "exit_price": 90.0,
                "quantity": 100
            })
        
        result = backtest.run()
        
        # 盈利因子 = 总盈利 / 总亏损 = 10000 / 5000 = 2.0
        assert abs(result.profit_factor - 2.0) < 0.1
    
    def test_sharpe_ratio_calculation(self):
        """测试夏普比率计算"""
        backtest = SimpleBacktest()
        
        # 添加一系列收益
        for i in range(20):
            backtest.add_signal({
                "code": "HK.00700",
                "action": "BUY",
                "entry_price": 100.0,
                "exit_price": 100.0 + (i % 5),
                "quantity": 100
            })
        
        result = backtest.run()
        
        assert result.sharpe_ratio is not None
    
    def test_max_drawdown_calculation(self):
        """测试最大回撤计算"""
        backtest = SimpleBacktest()
        
        # 先盈利后亏损
        backtest.add_signal({
            "code": "HK.00700",
            "action": "BUY",
            "entry_price": 100.0,
            "exit_price": 120.0,
            "quantity": 100
        })
        
        backtest.add_signal({
            "code": "HK.00700",
            "action": "BUY",
            "entry_price": 120.0,
            "exit_price": 100.0,
            "quantity": 100
        })
        
        result = backtest.run()
        
        assert result.max_drawdown >= 0
    
    def test_empty_signals(self):
        """测试无信号情况"""
        backtest = SimpleBacktest()
        
        result = backtest.run()
        
        assert result.total_trades == 0
        assert result.net_pnl == 0.0
        assert result.win_rate == 0.0
    
    def test_commission_deduction(self):
        """测试佣金扣除"""
        backtest = SimpleBacktest(commission_rate=0.001)  # 0.1% 佣金
        
        backtest.add_signal({
            "code": "HK.00700",
            "action": "BUY",
            "entry_price": 100.0,
            "exit_price": 100.0,  # 无盈亏
            "quantity": 100
        })
        
        result = backtest.run()
        
        # 即使无盈亏，也应有佣金成本
        assert result.net_pnl < 0
    
    def test_report_generation(self):
        """测试报告生成"""
        backtest = SimpleBacktest()
        
        for i in range(10):
            backtest.add_signal({
                "code": "HK.00700",
                "action": "BUY",
                "entry_price": 100.0,
                "exit_price": 100.0 + i,
                "quantity": 100
            })
        
        result = backtest.run()
        report = backtest.generate_report(result)
        
        # 报告结构检查
        assert "summary" in report
        assert "config" in report
        assert "trades" in report
        assert "capital_curve" in report
        assert report["summary"]["total_trades"] == 10
    
    def test_load_signals_from_dict(self):
        """测试从字典加载信号"""
        backtest = SimpleBacktest()
        
        signals = [
            {"code": "HK.00700", "action": "BUY", "entry_price": 100.0, "exit_price": 105.0, "quantity": 100},
            {"code": "HK.09988", "action": "BUY", "entry_price": 80.0, "exit_price": 82.0, "quantity": 200},
        ]
        
        backtest.load_signals(signals)
        
        assert len(backtest.signals) == 2
    
    def test_reset_backtest(self):
        """测试重置回测"""
        backtest = SimpleBacktest()
        
        backtest.add_signal({
            "code": "HK.00700",
            "action": "BUY",
            "entry_price": 100.0,
            "exit_price": 105.0,
            "quantity": 100
        })
        
        backtest.reset()
        
        assert len(backtest.signals) == 0
        assert len(backtest.trades) == 0


# 运行测试
if __name__ == "__main__":
    pytest.main([__file__, "-v"])
