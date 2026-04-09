"""
AutoResearch 搜索引擎

实现参数空间搜索算法：网格搜索、随机搜索、贝叶斯优化。
"""

import random
import numpy as np
from typing import Dict, List, Tuple, Optional, Any, Callable
from dataclasses import dataclass, field
from datetime import datetime
import logging

logger = logging.getLogger("ftseek.autoresearch")
logger.propagate = False


@dataclass
class ParameterSpace:
    """参数空间定义"""
    name: str
    param_type: str  # "int", "float", "categorical"
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    choices: Optional[List[Any]] = None
    step: Optional[float] = None
    
    def sample(self) -> Any:
        """随机采样参数值"""
        if self.param_type == "categorical":
            return random.choice(self.choices)
        elif self.param_type == "int":
            return random.randint(int(self.min_value), int(self.max_value))
        elif self.param_type == "float":
            return random.uniform(self.min_value, self.max_value)
        else:
            raise ValueError(f"Unknown param_type: {self.param_type}")
    
    def get_bounds(self) -> Tuple:
        """获取参数边界"""
        if self.param_type == "categorical":
            return (0, len(self.choices) - 1)
        else:
            return (self.min_value, self.max_value)


@dataclass
class SearchResult:
    """搜索结果"""
    strategy_name: str
    params: Dict[str, Any]
    win_rate: float
    total_return: float
    sharpe_ratio: float
    max_drawdown: float
    total_trades: int
    overfitting_score: float  # 过拟合评分 (0-1, 越小越好)
    train_metrics: Dict[str, float] = field(default_factory=dict)
    test_metrics: Dict[str, float] = field(default_factory=dict)
    search_method: str = ""
    search_iterations: int = 0
    search_time: float = 0.0
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    
    def summary(self) -> str:
        """生成结果摘要"""
        return (
            f"Strategy: {self.strategy_name}\n"
            f"Win Rate: {self.win_rate:.1%}\n"
            f"Sharpe Ratio: {self.sharpe_ratio:.2f}\n"
            f"Total Return: {self.total_return:.1%}\n"
            f"Max Drawdown: {self.max_drawdown:.1%}\n"
            f"Overfitting Score: {self.overfitting_score:.2f}\n"
            f"Method: {self.search_method} ({self.search_iterations} iterations)"
        )


class AutoResearch:
    """
    AutoResearch 策略参数自动研究
    
    基于 AutoResearch 项目原理，实现参数自动优化。
    
    使用示例:
        autoresearch = AutoResearch('confluence_v2', param_space)
        result = autoresearch.run(
            backtest_engine=backtest,
            train_data=train_data,
            test_data=test_data,
            method='bayesian',
            n_iterations=30,
        )
        print(result.summary())
    """
    
    def __init__(self, strategy_name: str, param_space: Dict[str, ParameterSpace]):
        """
        初始化 AutoResearch
        
        Args:
            strategy_name: 策略名称
            param_space: 参数空间定义
        """
        self.strategy_name = strategy_name
        self.param_space = param_space
        self.results: List[SearchResult] = []
        self.logger = logging.getLogger(f"ftseek.autoresearch.{strategy_name}")
    
    def grid_search(self, backtest_engine, train_data, test_data) -> SearchResult:
        """
        网格搜索
        
        遍历所有参数组合，找到最优参数。
        适用于参数空间较小的情况。
        """
        self.logger.info("Starting grid search...")
        start_time = datetime.now()
        
        # 生成所有参数组合
        param_combinations = self._generate_grid()
        self.logger.info(f"Total combinations: {len(param_combinations)}")
        
        best_result = None
        best_score = -float('inf')
        
        for i, params in enumerate(param_combinations):
            # 回测
            train_result = backtest_engine.run(params, train_data)
            
            # 样本外验证
            test_result = backtest_engine.run(params, test_data)
            
            # 计算综合评分
            score = self._calculate_score(train_result, test_result)
            
            # 过拟合检测
            overfit_score = self._detect_overfitting(train_result, test_result)
            
            # 更新最优结果
            if score > best_score and overfit_score < 0.3:
                best_score = score
                best_result = SearchResult(
                    strategy_name=self.strategy_name,
                    params=params,
                    win_rate=train_result.win_rate,
                    total_return=train_result.total_return,
                    sharpe_ratio=train_result.sharpe_ratio,
                    max_drawdown=train_result.max_drawdown,
                    total_trades=train_result.total_trades,
                    overfitting_score=overfit_score,
                    train_metrics={
                        'win_rate': train_result.win_rate,
                        'sharpe_ratio': train_result.sharpe_ratio,
                        'total_return': train_result.total_return,
                        'max_drawdown': train_result.max_drawdown,
                    },
                    test_metrics={
                        'win_rate': test_result.win_rate,
                        'sharpe_ratio': test_result.sharpe_ratio,
                        'total_return': test_result.total_return,
                        'max_drawdown': test_result.max_drawdown,
                    },
                    search_method='grid',
                    search_iterations=i + 1,
                    search_time=(datetime.now() - start_time).total_seconds(),
                )
                self.logger.info(
                    f"New best: score={score:.2f}, "
                    f"overfit={overfit_score:.2f}, "
                    f"params={params}"
                )
        
        self.results.append(best_result)
        return best_result
    
    def random_search(self, backtest_engine, train_data, test_data, 
                     n_iterations: int = 50) -> SearchResult:
        """
        随机搜索
        
        随机采样参数空间，找到最优参数。
        适用于参数空间较大的情况。
        """
        self.logger.info(f"Starting random search ({n_iterations} iterations)...")
        start_time = datetime.now()
        
        best_result = None
        best_score = -float('inf')
        
        for i in range(n_iterations):
            # 随机生成参数
            params = self._sample_random_params()
            
            # 回测
            train_result = backtest_engine.run(params, train_data)
            test_result = backtest_engine.run(params, test_data)
            
            score = self._calculate_score(train_result, test_result)
            overfit_score = self._detect_overfitting(train_result, test_result)
            
            if score > best_score and overfit_score < 0.3:
                best_score = score
                best_result = SearchResult(
                    strategy_name=self.strategy_name,
                    params=params,
                    win_rate=train_result.win_rate,
                    total_return=train_result.total_return,
                    sharpe_ratio=train_result.sharpe_ratio,
                    max_drawdown=train_result.max_drawdown,
                    total_trades=train_result.total_trades,
                    overfitting_score=overfit_score,
                    train_metrics={
                        'win_rate': train_result.win_rate,
                        'sharpe_ratio': train_result.sharpe_ratio,
                        'total_return': train_result.total_return,
                        'max_drawdown': train_result.max_drawdown,
                    },
                    test_metrics={
                        'win_rate': test_result.win_rate,
                        'sharpe_ratio': test_result.sharpe_ratio,
                        'total_return': test_result.total_return,
                        'max_drawdown': test_result.max_drawdown,
                    },
                    search_method='random',
                    search_iterations=i + 1,
                    search_time=(datetime.now() - start_time).total_seconds(),
                )
                self.logger.info(
                    f"Iteration {i+1}/{n_iterations}: "
                    f"score={score:.2f}, overfit={overfit_score:.2f}"
                )
        
        self.results.append(best_result)
        return best_result
    
    def bayesian_optimization(self, backtest_engine, train_data, test_data,
                             n_iterations: int = 30) -> SearchResult:
        """
        贝叶斯优化
        
        使用历史表现指导参数搜索。
        适用于参数空间较大且搜索成本高的情况。
        """
        self.logger.info(f"Starting bayesian optimization ({n_iterations} iterations)...")
        start_time = datetime.now()
        
        try:
            from skopt import gp_minimize
        except ImportError:
            self.logger.warning("scikit-optimize not installed, falling back to random search")
            return self.random_search(backtest_engine, train_data, test_data, n_iterations)
        
        def objective(params):
            """优化目标函数"""
            params_dict = self._convert_to_dict(params)
            train_result = backtest_engine.run(params_dict, train_data)
            return -train_result.sharpe_ratio  # 最小化负夏普比率
        
        # 获取参数边界
        param_bounds = self._get_param_bounds()
        
        # 运行贝叶斯优化
        result = gp_minimize(objective, param_bounds, n_calls=n_iterations, random_state=42)
        
        # 验证最优参数
        best_params = self._convert_to_dict(result.x)
        train_result = backtest_engine.run(best_params, train_data)
        test_result = backtest_engine.run(best_params, test_data)
        
        overfit_score = self._detect_overfitting(train_result, test_result)
        
        best_result = SearchResult(
            strategy_name=self.strategy_name,
            params=best_params,
            win_rate=train_result.win_rate,
            total_return=train_result.total_return,
            sharpe_ratio=train_result.sharpe_ratio,
            max_drawdown=train_result.max_drawdown,
            total_trades=train_result.total_trades,
            overfitting_score=overfit_score,
            train_metrics={
                'win_rate': train_result.win_rate,
                'sharpe_ratio': train_result.sharpe_ratio,
                'total_return': train_result.total_return,
                'max_drawdown': train_result.max_drawdown,
            },
            test_metrics={
                'win_rate': test_result.win_rate,
                'sharpe_ratio': test_result.sharpe_ratio,
                'total_return': test_result.total_return,
                'max_drawdown': test_result.max_drawdown,
            },
            search_method='bayesian',
            search_iterations=n_iterations,
            search_time=(datetime.now() - start_time).total_seconds(),
        )
        
        self.results.append(best_result)
        return best_result
    
    def _generate_grid(self) -> List[Dict[str, Any]]:
        """生成网格搜索的所有参数组合"""
        import itertools
        
        param_values = {}
        for name, space in self.param_space.items():
            if space.param_type == "categorical":
                param_values[name] = space.choices
            elif space.param_type == "int":
                param_values[name] = list(range(
                    int(space.min_value),
                    int(space.max_value) + 1,
                    int(space.step) if space.step else 1
                ))
            elif space.param_type == "float":
                param_values[name] = list(np.arange(
                    space.min_value,
                    space.max_value + space.step / 2,
                    space.step
                ))
        
        # 生成所有组合
        keys = param_values.keys()
        values = param_values.values()
        combinations = list(itertools.product(*values))
        
        return [dict(zip(keys, combo)) for combo in combinations]
    
    def _sample_random_params(self) -> Dict[str, Any]:
        """随机采样参数"""
        return {name: space.sample() for name, space in self.param_space.items()}
    
    def _convert_to_dict(self, params: List) -> Dict[str, Any]:
        """将参数列表转换为字典"""
        return {name: value for name, value in zip(self.param_space.keys(), params)}
    
    def _get_param_bounds(self) -> List[Tuple]:
        """获取参数边界 (用于贝叶斯优化)"""
        return [space.get_bounds() for space in self.param_space.values()]
    
    def _calculate_score(self, train_result, test_result) -> float:
        """计算综合评分"""
        # 夏普比率权重 40% + 胜率权重 30% + 总收益权重 30%
        score = (
            train_result.sharpe_ratio * 0.4 +
            train_result.win_rate * 100 * 0.3 +
            train_result.total_return * 0.3
        )
        
        # 样本外表现折扣
        oos_ratio = test_result.sharpe_ratio / max(train_result.sharpe_ratio, 0.01)
        score *= oos_ratio
        
        return score
    
    def _detect_overfitting(self, train_result, test_result) -> float:
        """检测过拟合"""
        metrics = ['sharpe_ratio', 'win_rate', 'total_return', 'max_drawdown']
        
        differences = []
        for metric in metrics:
            train_val = getattr(train_result, metric, 0)
            test_val = getattr(test_result, metric, 0)
            
            # 计算相对差异
            if train_val != 0:
                diff = abs(test_val - train_val) / abs(train_val)
                differences.append(diff)
        
        # 平均差异作为过拟合评分
        overfit_score = np.mean(differences) if differences else 0
        return overfit_score
