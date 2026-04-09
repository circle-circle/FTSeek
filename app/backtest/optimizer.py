"""
ParameterOptimizer - 参数优化器

功能:
- 网格搜索
- 贝叶斯优化
- 遗传算法
"""

import numpy as np
from typing import Dict, List, Any, Callable, Tuple, Optional
from dataclasses import dataclass
import logging
from scipy.optimize import minimize

logger = logging.getLogger("ftseek.backtest.optimizer")
logger.propagate = False  # 防止日志重复


@dataclass
class OptimizationResult:
    """优化结果"""
    best_params: Dict[str, Any]
    best_score: float
    all_results: List[Dict]
    n_evaluations: int
    method: str


class GridSearchOptimizer:
    """网格搜索优化器"""
    
    def __init__(self, param_grid: Dict[str, List[Any]]):
        """
        初始化网格搜索
        
        Args:
            param_grid: 参数网格
                {'stop_loss': [0.05, 0.08, 0.10], 'take_profit': [0.10, 0.15]}
        """
        self.param_grid = param_grid
    
    def optimize(self, objective: Callable, n_jobs: int = 1) -> OptimizationResult:
        """
        执行网格搜索
        
        Args:
            objective: 目标函数，接收参数字典，返回分数
            n_jobs: 并行作业数
        
        Returns:
            优化结果
        """
        from itertools import product
        
        # 生成所有参数组合
        keys = list(self.param_grid.keys())
        values = list(self.param_grid.values())
        param_combinations = list(product(*values))
        
        logger.info(f"Grid search: {len(param_combinations)} combinations")
        
        all_results = []
        best_score = float('-inf')
        best_params = {}
        
        for i, combination in enumerate(param_combinations):
            params = dict(zip(keys, combination))
            
            try:
                score = objective(params)
                
                result = {
                    'params': params,
                    'score': score,
                    'rank': i + 1
                }
                all_results.append(result)
                
                if score > best_score:
                    best_score = score
                    best_params = params
                
                if (i + 1) % 10 == 0:
                    logger.info(f"Progress: {i + 1}/{len(param_combinations)}")
                
            except Exception as e:
                logger.error(f"Error evaluating params {params}: {e}")
        
        return OptimizationResult(
            best_params=best_params,
            best_score=best_score,
            all_results=all_results,
            n_evaluations=len(all_results),
            method='grid_search'
        )


class BayesianOptimizer:
    """贝叶斯优化器"""
    
    def __init__(self, param_bounds: Dict[str, Tuple[float, float]]):
        """
        初始化贝叶斯优化
        
        Args:
            param_bounds: 参数边界
                {'stop_loss': (0.05, 0.15), 'take_profit': (0.10, 0.25)}
        """
        self.param_bounds = param_bounds
        self.results = []
    
    def optimize(self, objective: Callable, n_calls: int = 50) -> OptimizationResult:
        """
        执行贝叶斯优化
        
        Args:
            objective: 目标函数
            n_calls: 评估次数
        
        Returns:
            优化结果
        """
        try:
            from skopt import gp_minimize
            from skopt.callbacks import DeltaXStopper
        except ImportError:
            logger.warning("scikit-optimize not installed, falling back to random search")
            return self._random_search(objective, n_calls)
        
        # 准备参数边界
        keys = list(self.param_bounds.keys())
        bounds = [self.param_bounds[k] for k in keys]
        
        logger.info(f"Bayesian optimization: {n_calls} evaluations")
        
        def objective_wrapper(x):
            params = dict(zip(keys, x))
            try:
                score = objective(params)
                self.results.append({'params': params, 'score': score})
                return -score  # 最小化负分数
            except Exception as e:
                logger.error(f"Error: {e}")
                return float('inf')
        
        # 执行优化
        result = gp_minimize(
            objective_wrapper,
            bounds,
            n_calls=n_calls,
            acq_func='EI',  # Expected Improvement
            random_state=42,
            callback=[DeltaXStopper(0.01)]
        )
        
        # 提取最佳参数
        best_params = dict(zip(keys, result.x))
        best_score = -result.fun
        
        return OptimizationResult(
            best_params=best_params,
            best_score=best_score,
            all_results=self.results,
            n_evaluations=len(self.results),
            method='bayesian'
        )
    
    def _random_search(self, objective: Callable, n_calls: int) -> OptimizationResult:
        """随机搜索（备用方案）"""
        keys = list(self.param_bounds.keys())
        
        all_results = []
        best_score = float('-inf')
        best_params = {}
        
        for i in range(n_calls):
            # 随机采样参数
            params = {
                k: np.random.uniform(*v)
                for k, v in self.param_bounds.items()
            }
            
            try:
                score = objective(params)
                result = {'params': params, 'score': score}
                all_results.append(result)
                
                if score > best_score:
                    best_score = score
                    best_params = params
                
            except Exception as e:
                logger.error(f"Error: {e}")
        
        return OptimizationResult(
            best_params=best_params,
            best_score=best_score,
            all_results=all_results,
            n_evaluations=len(all_results),
            method='random_search'
        )


class GeneticOptimizer:
    """遗传算法优化器"""
    
    def __init__(
        self,
        param_bounds: Dict[str, Tuple[float, float]],
        population_size: int = 50,
        generations: int = 100
    ):
        """
        初始化遗传算法
        
        Args:
            param_bounds: 参数边界
            population_size: 种群大小
            generations: 代数
        """
        self.param_bounds = param_bounds
        self.population_size = population_size
        self.generations = generations
    
    def optimize(self, objective: Callable) -> OptimizationResult:
        """
        执行遗传算法优化
        
        Args:
            objective: 目标函数
        
        Returns:
            优化结果
        """
        keys = list(self.param_bounds.keys())
        n_params = len(keys)
        
        logger.info(f"Genetic algorithm: {self.population_size} individuals, {self.generations} generations")
        
        # 初始化种群
        population = self._initialize_population(n_params)
        
        all_results = []
        best_individual = None
        best_fitness = float('-inf')
        
        for generation in range(self.generations):
            # 评估适应度
            fitness_scores = []
            for individual in population:
                params = dict(zip(keys, individual))
                try:
                    fitness = objective(params)
                    fitness_scores.append(fitness)
                    all_results.append({'params': params, 'score': fitness, 'generation': generation})
                    
                    if fitness > best_fitness:
                        best_fitness = fitness
                        best_individual = individual.copy()
                
                except Exception as e:
                    logger.error(f"Error: {e}")
                    fitness_scores.append(float('-inf'))
            
            logger.info(f"Generation {generation + 1}/{self.generations}, best: {best_fitness:.4f}")
            
            # 选择、交叉、变异
            population = self._evolve(population, fitness_scores)
        
        best_params = dict(zip(keys, best_individual)) if best_individual is not None else {}
        
        return OptimizationResult(
            best_params=best_params,
            best_score=best_fitness,
            all_results=all_results,
            n_evaluations=len(all_results),
            method='genetic'
        )
    
    def _initialize_population(self, n_params: int) -> List[List[float]]:
        """初始化种群"""
        population = []
        for _ in range(self.population_size):
            individual = [
                np.random.uniform(*self.param_bounds[list(self.param_bounds.keys())[i]])
                for i in range(n_params)
            ]
            population.append(individual)
        return population
    
    def _evolve(self, population: List[List[float]], fitness_scores: List[float]) -> List[List[float]]:
        """进化操作"""
        # 选择（锦标赛选择）
        selected = self._tournament_selection(population, fitness_scores)
        
        # 交叉
        offspring = self._crossover(selected)
        
        # 变异
        offspring = self._mutate(offspring)
        
        return offspring
    
    def _tournament_selection(self, population: List[List[float]], fitness_scores: List[float], tournament_size: int = 5) -> List[List[float]]:
        """锦标赛选择"""
        selected = []
        for _ in range(len(population)):
            contestants = np.random.choice(len(population), size=tournament_size, replace=False)
            winner_idx = contestants[np.argmax([fitness_scores[i] for i in contestants])]
            selected.append(population[winner_idx].copy())
        return selected
    
    def _crossover(self, population: List[List[float]], crossover_rate: float = 0.8) -> List[List[float]]:
        """交叉操作"""
        offspring = []
        for i in range(0, len(population), 2):
            if i + 1 < len(population) and np.random.random() < crossover_rate:
                # 单点交叉
                point = np.random.randint(1, len(population[0]))
                child1 = population[i][:point] + population[i+1][point:]
                child2 = population[i+1][:point] + population[i][point:]
                offspring.append(child1)
                offspring.append(child2)
            else:
                offspring.append(population[i].copy())
                if i + 1 < len(population):
                    offspring.append(population[i+1].copy())
        return offspring
    
    def _mutate(self, population: List[List[float]], mutation_rate: float = 0.1) -> List[List[float]]:
        """变异操作"""
        keys = list(self.param_bounds.keys())
        for individual in population:
            for i in range(len(individual)):
                if np.random.random() < mutation_rate:
                    # 高斯变异
                    individual[i] += np.random.normal(0, 0.1)
                    # 边界检查
                    individual[i] = np.clip(individual[i], *self.param_bounds[keys[i]])
        return population
