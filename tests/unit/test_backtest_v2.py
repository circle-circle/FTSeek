"""
Backtest V2 Tests - 增强版回测测试
"""

import pytest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from datetime import datetime, timedelta
from app.backtest.backtest_engine import (
    BacktestEngine,
    BacktestConfig,
    Trade,
    MonteCarloSimulator
)
from app.backtest.optimizer import (
    GridSearchOptimizer,
    BayesianOptimizer,
    GeneticOptimizer
)


class TestBacktestEngine:
    """测试回测引擎"""
    
    def setup_method(self):
        """每个测试前执行"""
        self.config = BacktestConfig(
            initial_capital=100000.0,
            commission_rate=0.0003,
            risk_free_rate=0.02
        )
        self.engine = BacktestEngine(self.config)
        
        # 添加测试交易
        base_time = datetime(2026, 1, 1)
        for i in range(10):
            trade = Trade(
                entry_time=base_time + timedelta(days=i*2),
                exit_time=base_time + timedelta(days=i*2+1),
                symbol='AAPL',
                direction='LONG',
                entry_price=150.0 + i,
                exit_price=155.0 + i if i % 3 != 0 else 148.0 + i,  # 70% 胜率
                quantity=100
            )
            trade.pnl = (trade.exit_price - trade.entry_price) * trade.quantity
            trade.pnl_percent = trade.pnl / (trade.entry_price * trade.quantity)
            self.engine.add_trade(trade)
    
    def test_calculate_total_return(self):
        """测试总收益率计算"""
        result = self.engine.calculate_metrics()
        assert result.total_trades == 10
        assert result.total_return != 0.0
    
    def test_calculate_sharpe_ratio(self):
        """测试 Sharpe 比率计算"""
        result = self.engine.calculate_metrics()
        assert result.sharpe_ratio is not None
    
    def test_calculate_sortino_ratio(self):
        """测试 Sortino 比率计算"""
        result = self.engine.calculate_metrics()
        assert result.sortino_ratio is not None
    
    def test_calculate_max_drawdown(self):
        """测试最大回撤计算"""
        result = self.engine.calculate_metrics()
        assert 0.0 <= result.max_drawdown <= 1.0
    
    def test_calculate_win_rate(self):
        """测试胜率计算"""
        result = self.engine.calculate_metrics()
        assert 0.0 <= result.win_rate <= 1.0
        assert result.win_rate > 0.5  # 70% 胜率
    
    def test_calculate_profit_factor(self):
        """测试盈亏比计算"""
        result = self.engine.calculate_metrics()
        assert result.profit_factor > 0.0
    
    def test_calculate_var(self):
        """测试 VaR 计算"""
        result = self.engine.calculate_metrics()
        assert result.var_95 <= 0.0  # VaR 应该是负值（亏损）
    
    def test_monthly_returns(self):
        """测试月度收益计算"""
        result = self.engine.calculate_metrics()
        assert isinstance(result.monthly_returns, dict)
    
    def test_consecutive_stats(self):
        """测试连续统计"""
        result = self.engine.calculate_metrics()
        assert result.max_consecutive_wins >= 0
        assert result.max_consecutive_losses >= 0


class TestMonteCarloSimulator:
    """测试蒙特卡洛模拟器"""
    
    def setup_method(self):
        """每个测试前执行"""
        self.config = BacktestConfig()
        self.engine = BacktestEngine(self.config)
        
        # 添加测试交易
        base_time = datetime(2026, 1, 1)
        for i in range(20):
            trade = Trade(
                entry_time=base_time + timedelta(days=i),
                exit_time=base_time + timedelta(days=i+1),
                symbol='AAPL',
                direction='LONG',
                entry_price=150.0,
                exit_price=150.0 * (1 + 0.01 * (i % 5 - 2)),  # 随机收益
                quantity=100
            )
            trade.pnl = (trade.exit_price - trade.entry_price) * trade.quantity
            trade.pnl_percent = trade.pnl / (trade.entry_price * trade.quantity)
            self.engine.add_trade(trade)
        
        self.simulator = MonteCarloSimulator(self.engine)
    
    def test_run_simulation(self):
        """测试蒙特卡洛模拟"""
        results = self.simulator.run_simulation(n_simulations=100)
        
        assert 'n_simulations' in results
        assert results['n_simulations'] == 100
        assert 'total_return' in results
        assert 'mean' in results['total_return']
    
    def test_stress_test(self):
        """测试压力测试"""
        scenario = {
            'name': 'Market Crash',
            'market_drop': -0.30,
            'volatility_increase': 2.0
        }
        
        result = self.simulator.run_stress_test(scenario)
        
        assert result is not None
        assert result.max_drawdown > 0.0


class TestGridSearchOptimizer:
    """测试网格搜索优化器"""
    
    def test_grid_search(self):
        """测试网格搜索"""
        param_grid = {
            'stop_loss': [0.05, 0.08, 0.10],
            'take_profit': [0.10, 0.15, 0.20]
        }
        
        optimizer = GridSearchOptimizer(param_grid)
        
        # 简单目标函数
        def objective(params):
            return params['stop_loss'] * params['take_profit']
        
        result = optimizer.optimize(objective)
        
        assert result.best_params is not None
        assert result.best_score > 0.0
        assert result.n_evaluations == 9  # 3x3


class TestBayesianOptimizer:
    """测试贝叶斯优化器"""
    
    def test_bayesian_optimization(self):
        """测试贝叶斯优化"""
        param_bounds = {
            'stop_loss': (0.05, 0.15),
            'take_profit': (0.10, 0.25)
        }
        
        optimizer = BayesianOptimizer(param_bounds)
        
        def objective(params):
            return -(params['stop_loss'] - 0.10) ** 2 - (params['take_profit'] - 0.15) ** 2
        
        result = optimizer.optimize(objective, n_calls=20)
        
        assert result.best_params is not None
        assert result.n_evaluations <= 20


class TestGeneticOptimizer:
    """测试遗传算法优化器"""
    
    def test_genetic_algorithm(self):
        """测试遗传算法"""
        param_bounds = {
            'param1': (0.0, 1.0),
            'param2': (0.0, 1.0)
        }
        
        optimizer = GeneticOptimizer(
            param_bounds,
            population_size=20,
            generations=10
        )
        
        def objective(params):
            return params['param1'] ** 2 + params['param2'] ** 2
        
        result = optimizer.optimize(objective)
        
        assert result.best_params is not None
        assert result.n_evaluations == 200  # 20 * 10


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
