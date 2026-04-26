"""
AutoResearch 回测集成

集成到现有回测系统，实现策略参数自动优化。
"""

from typing import Dict, Optional, List, Any
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging

from .search_engine import AutoResearch, ParameterSpace, SearchResult
from .overfitting_detector import OverfittingDetector

logger = logging.getLogger("ftseek.autoresearch.backtest")
logger.propagate = False


@dataclass
class BacktestResult:
    """回测结果"""
    win_rate: float
    total_return: float
    sharpe_ratio: float
    max_drawdown: float
    total_trades: int
    trades: List[Dict[str, Any]] = None


class AutoResearchBacktest:
    """
    AutoResearch 回测
    
    集成到现有回测系统，实现策略参数自动优化。
    
    使用示例:
        backtest = AutoResearchBacktest('confluence_v2', initial_capital=100000)
        result = backtest.run_optimization(
            train_start='2025-01-01',
            train_end='2025-12-31',
            test_start='2026-01-01',
            test_end='2026-03-29',
            method='bayesian',
            n_iterations=30,
        )
        print(result.summary())
    """
    
    def __init__(self, strategy_name: str, initial_capital: float = 100000):
        """
        初始化 AutoResearch 回测
        
        Args:
            strategy_name: 策略名称
            initial_capital: 初始资金
        """
        self.strategy_name = strategy_name
        self.initial_capital = initial_capital
        self.backtest_engine = None  # 待集成现有回测引擎
        self.logger = logging.getLogger(f"ftseek.autoresearch.backtest.{strategy_name}")
    
    def run_optimization(self, train_start: str, train_end: str,
                        test_start: str, test_end: str,
                        method: str = 'bayesian',
                        n_iterations: int = 30) -> SearchResult:
        """
        运行参数优化
        
        Args:
            train_start: 训练集开始日期
            train_end: 训练集结束日期
            test_start: 测试集开始日期
            test_end: 测试集结束日期
            method: 搜索方法 ('grid', 'random', 'bayesian')
            n_iterations: 迭代次数
        
        Returns:
            最优参数和结果
        """
        self.logger.info(
            f"Starting optimization: {self.strategy_name}, "
            f"method={method}, iterations={n_iterations}"
        )
        
        # 加载数据
        train_data = self._load_data(train_start, train_end)
        test_data = self._load_data(test_start, test_end)
        
        # 创建 AutoResearch
        param_space = self._get_param_space()
        autoresearch = AutoResearch(self.strategy_name, param_space)
        
        # 运行优化
        if method == 'grid':
            result = autoresearch.grid_search(self.backtest_engine, train_data, test_data)
        elif method == 'random':
            result = autoresearch.random_search(
                self.backtest_engine, train_data, test_data, n_iterations
            )
        elif method == 'bayesian':
            result = autoresearch.bayesian_optimization(
                self.backtest_engine, train_data, test_data, n_iterations
            )
        else:
            raise ValueError(f"Unknown method: {method}")
        
        self.logger.info(f"Optimization completed: {result.summary()}")
        
        return result
    
    def _load_data(self, start_date: str, end_date: str) -> Any:
        """
        加载回测数据
        
        Args:
            start_date: 开始日期
            end_date: 结束日期
        
        Returns:
            回测数据
        """
        # P1 修复 (2026-04-12): 已规划集成现有回测引擎的数据加载
        # 注：完整实现需要集成 app/data_subscriber.py 或 app/backtest/ 模块
        self.logger.info(f"Loading data: {start_date} to {end_date}")
        return None
    
    def _get_param_space(self) -> Dict[str, ParameterSpace]:
        """获取参数空间"""
        if self.strategy_name == 'confluence_v2':
            return {
                'min_factors': ParameterSpace('min_factors', 'int', 2, 4),
                'rsi_period': ParameterSpace('rsi_period', 'int', 10, 20),
                'rsi_overbought': ParameterSpace('rsi_overbought', 'int', 65, 80),
                'rsi_oversold': ParameterSpace('rsi_oversold', 'int', 20, 35),
                'macd_fast': ParameterSpace('macd_fast', 'int', 8, 16),
                'macd_slow': ParameterSpace('macd_slow', 'int', 20, 32),
                'bb_period': ParameterSpace('bb_period', 'int', 15, 25),
                'bb_std': ParameterSpace('bb_std', 'float', 1.5, 2.5, step=0.1),
                'st_multiplier': ParameterSpace('st_multiplier', 'float', 2.5, 3.5, step=0.1),
            }
        elif self.strategy_name == 'supertrend':
            return {
                'atr_period': ParameterSpace('atr_period', 'int', 8, 14),
                'base_multiplier': ParameterSpace('base_multiplier', 'float', 2.0, 3.5, step=0.1),
                'range_adjust_factor': ParameterSpace('range_adjust_factor', 'float', 1.0, 1.5, step=0.1),
            }
        elif self.strategy_name == 'sentiment_ai':
            return {
                'lookback_hours': ParameterSpace('lookback_hours', 'int', 12, 48),
                'max_news': ParameterSpace('max_news', 'int', 5, 20),
                'sentiment_threshold': ParameterSpace('sentiment_threshold', 'float', 0.2, 0.5, step=0.05),
            }
        else:
            raise ValueError(f"Unknown strategy: {self.strategy_name}")
    
    def generate_report(self, result: SearchResult) -> str:
        """
        生成优化报告
        
        Args:
            result: 优化结果
        
        Returns:
            报告文本
        """
        lines = [
            "=" * 70,
            f"AutoResearch 优化报告 - {self.strategy_name}",
            "=" * 70,
            "",
            result.summary(),
            "",
            "最优参数:",
        ]
        
        for param, value in result.params.items():
            lines.append(f"  {param}: {value}")
        
        lines.extend([
            "",
            "训练集表现:",
        ])
        for metric, value in result.train_metrics.items():
            lines.append(f"  {metric}: {value:.4f}" if isinstance(value, float) else f"  {metric}: {value}")
        
        lines.extend([
            "",
            "测试集表现:",
        ])
        for metric, value in result.test_metrics.items():
            lines.append(f"  {metric}: {value:.4f}" if isinstance(value, float) else f"  {metric}: {value}")
        
        lines.extend([
            "",
            "=" * 70,
        ])
        
        return "\n".join(lines)
