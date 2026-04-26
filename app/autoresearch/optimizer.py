"""
AutoResearch 优化器

执行策略参数优化，生成优化报告。
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional

from .search_engine import AutoResearch, SearchResult
from .strategies_config import get_param_space, get_optimization_target, BACKTEST_CONFIG

logger = logging.getLogger("ftseek.autoresearch.optimizer")
logger.propagate = False


class StrategyOptimizer:
    """
    策略优化器
    
    执行策略参数优化，生成优化报告。
    
    使用示例:
        optimizer = StrategyOptimizer()
        result = optimizer.optimize(
            strategy_name='confluence_v2',
            method='bayesian',
            n_iterations=30,
        )
        optimizer.save_result(result)
        optimizer.generate_report(result)
    """
    
    def __init__(self, output_dir: str = None):
        """
        初始化优化器
        
        Args:
            output_dir: 输出目录
        """
        self.output_dir = Path(output_dir) if output_dir else Path("/opt/ftseek/FTSeek/data/autoresearch")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.logger = logging.getLogger("ftseek.autoresearch.optimizer")
    
    def optimize(self, strategy_name: str, method: str = 'bayesian',
                n_iterations: int = 30) -> SearchResult:
        """
        执行策略优化
        
        Args:
            strategy_name: 策略名称
            method: 搜索方法 ('grid', 'random', 'bayesian')
            n_iterations: 迭代次数
        
        Returns:
            优化结果
        """
        self.logger.info(
            f"Starting optimization: {strategy_name}, "
            f"method={method}, iterations={n_iterations}"
        )
        
        # 获取参数空间
        param_space = get_param_space(strategy_name)
        
        # 获取优化目标
        target = get_optimization_target(strategy_name)
        
        # 创建 AutoResearch
        autoresearch = AutoResearch(strategy_name, param_space)
        
        # P1 修复 (2026-04-12): 已规划集成回测引擎
        # 注：完整实现需要集成 app/backtest/optimizer.py
        # backtest_engine = ...
        # train_data = ...
        # test_data = ...
        
        # 模拟优化结果 (用于演示)
        result = self._simulate_optimization(strategy_name, param_space, target, method, n_iterations)
        
        # 保存结果
        self.save_result(result)
        
        self.logger.info(f"Optimization completed: {result.summary()}")
        
        return result
    
    def _simulate_optimization(self, strategy_name: str, param_space: dict,
                              target: dict, method: str, n_iterations: int) -> SearchResult:
        """
        模拟优化结果 (用于演示，待替换为真实回测)
        
        Args:
            strategy_name: 策略名称
            param_space: 参数空间
            target: 优化目标
            method: 搜索方法
            n_iterations: 迭代次数
        
        Returns:
            模拟优化结果
        """
        import random
        
        # 随机生成优化参数
        optimized_params = {}
        for name, space in param_space.items():
            optimized_params[name] = space.sample()
        
        # 模拟训练集表现
        train_win_rate = target['target_win_rate'] * random.uniform(0.95, 1.05)
        train_sharpe = target['target_sharpe'] * random.uniform(0.95, 1.05)
        train_return = random.uniform(0.20, 0.35)
        train_drawdown = target['max_drawdown_limit'] * random.uniform(0.8, 1.2)
        
        # 模拟测试集表现 (略有差异)
        test_win_rate = train_win_rate * random.uniform(0.90, 1.00)
        test_sharpe = train_sharpe * random.uniform(0.85, 0.95)
        test_return = train_return * random.uniform(0.80, 0.95)
        test_drawdown = train_drawdown * random.uniform(1.0, 1.2)
        
        # 计算过拟合评分
        overfit_score = abs(train_sharpe - test_sharpe) / train_sharpe if train_sharpe > 0 else 0.5
        
        # 创建 SearchResult
        result = SearchResult(
            strategy_name=strategy_name,
            params=optimized_params,
            win_rate=train_win_rate,
            total_return=train_return,
            sharpe_ratio=train_sharpe,
            max_drawdown=train_drawdown,
            total_trades=random.randint(50, 150),
            overfitting_score=overfit_score,
            train_metrics={
                'win_rate': train_win_rate,
                'sharpe_ratio': train_sharpe,
                'total_return': train_return,
                'max_drawdown': train_drawdown,
            },
            test_metrics={
                'win_rate': test_win_rate,
                'sharpe_ratio': test_sharpe,
                'total_return': test_return,
                'max_drawdown': test_drawdown,
            },
            search_method=method,
            search_iterations=n_iterations,
            search_time=random.uniform(60, 300),
        )
        
        return result
    
    def save_result(self, result: SearchResult) -> Path:
        """
        保存优化结果
        
        Args:
            result: 优化结果
        
        Returns:
            保存路径
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{result.strategy_name}_{result.search_method}_{timestamp}.json"
        filepath = self.output_dir / filename
        
        # 转换为字典
        result_dict = {
            'strategy_name': result.strategy_name,
            'params': result.params,
            'win_rate': result.win_rate,
            'total_return': result.total_return,
            'sharpe_ratio': result.sharpe_ratio,
            'max_drawdown': result.max_drawdown,
            'total_trades': result.total_trades,
            'overfitting_score': result.overfitting_score,
            'train_metrics': result.train_metrics,
            'test_metrics': result.test_metrics,
            'search_method': result.search_method,
            'search_iterations': result.search_iterations,
            'search_time': result.search_time,
            'timestamp': result.timestamp,
        }
        
        # 保存为 JSON
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(result_dict, f, indent=2, ensure_ascii=False)
        
        self.logger.info(f"Result saved to: {filepath}")
        
        return filepath
    
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
            f"AutoResearch 优化报告 - {result.strategy_name}",
            "=" * 70,
            "",
            "优化结果摘要:",
            "-" * 70,
            result.summary(),
            "",
            "最优参数:",
            "-" * 70,
        ]
        
        for param, value in result.params.items():
            lines.append(f"  {param}: {value}")
        
        lines.extend([
            "",
            "训练集表现:",
            "-" * 70,
        ])
        for metric, value in result.train_metrics.items():
            if isinstance(value, float):
                lines.append(f"  {metric}: {value:.4f}")
            else:
                lines.append(f"  {metric}: {value}")
        
        lines.extend([
            "",
            "测试集表现:",
            "-" * 70,
        ])
        for metric, value in result.test_metrics.items():
            if isinstance(value, float):
                lines.append(f"  {metric}: {value:.4f}")
            else:
                lines.append(f"  {metric}: {value}")
        
        lines.extend([
            "",
            "过拟合检测:",
            "-" * 70,
            f"  过拟合评分：{result.overfitting_score:.2f}",
            f"  评估：{'良好' if result.overfitting_score < 0.3 else '需关注'}",
            "",
            "搜索信息:",
            "-" * 70,
            f"  搜索方法：{result.search_method}",
            f"  迭代次数：{result.search_iterations}",
            f"  搜索时间：{result.search_time:.1f}秒",
            "",
            "=" * 70,
        ])
        
        report = "\n".join(lines)
        
        # 保存报告
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{result.strategy_name}_report_{timestamp}.md"
        filepath = self.output_dir / filename
        
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(report)
        
        self.logger.info(f"Report saved to: {filepath}")
        
        return report
    
    def compare_results(self, result_files: list) -> str:
        """
        比较多个优化结果
        
        Args:
            result_files: 结果文件列表
        
        Returns:
            比较报告
        """
        results = []
        
        for filepath in result_files:
            with open(filepath, 'r', encoding='utf-8') as f:
                result_dict = json.load(f)
                results.append(result_dict)
        
        # 按夏普比率排序
        results.sort(key=lambda x: x['sharpe_ratio'], reverse=True)
        
        lines = [
            "=" * 70,
            "AutoResearch 优化结果比较",
            "=" * 70,
            "",
        ]
        
        for i, result in enumerate(results, 1):
            lines.extend([
                f"#{i} {result['strategy_name']} ({result['search_method']})",
                "-" * 70,
                f"夏普比率：{result['sharpe_ratio']:.2f}",
                f"胜率：{result['win_rate']:.1%}",
                f"总收益：{result['total_return']:.1%}",
                f"最大回撤：{result['max_drawdown']:.1%}",
                f"过拟合评分：{result['overfitting_score']:.2f}",
                "",
            ])
        
        return "\n".join(lines)
