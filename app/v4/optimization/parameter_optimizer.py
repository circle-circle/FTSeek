"""
ParameterOptimizer - 参数优化模块

功能:
- 网格搜索
- 交叉验证
- 避免过拟合 (复杂度惩罚)
- 参数敏感性分析
"""
import logging
from typing import Dict, List, Optional, Any, Callable, Tuple
from dataclasses import dataclass, field
from datetime import datetime
import math
import itertools

logger = logging.getLogger("ftseek.v4.parameter_optimizer")
logger.propagate = False  # 防止日志重复


@dataclass
class ParameterRange:
    """参数范围"""
    name: str
    min_val: float
    max_val: float
    step: float
    
    def get_values(self) -> List[float]:
        """获取参数值列表"""
        values = []
        current = self.min_val
        while current <= self.max_val + 1e-9:  # 浮点误差
            values.append(round(current, 6))
            current += self.step
        return values
    
    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "min_val": self.min_val,
            "max_val": self.max_val,
            "step": self.step,
            "num_values": len(self.get_values()),
        }


@dataclass
class OptimizationResult:
    """优化结果"""
    best_params: Dict[str, float]
    best_score: float
    total_trials: int
    sharpe_ratio: float
    max_drawdown: float
    baseline_score: float = 0.0
    all_trials: List[Dict[str, Any]] = field(default_factory=list)
    
    def get_improvement(self) -> float:
        """获取改进幅度"""
        if self.baseline_score == 0:
            return 0.0
        return (self.best_score - self.baseline_score) / self.baseline_score
    
    def to_dict(self) -> dict:
        return {
            "best_params": self.best_params,
            "best_score": self.best_score,
            "total_trials": self.total_trials,
            "sharpe_ratio": self.sharpe_ratio,
            "max_drawdown": self.max_drawdown,
            "baseline_score": self.baseline_score,
            "improvement": f"{self.get_improvement():.1%}",
        }


class GridSearch:
    """网格搜索"""
    
    def __init__(self):
        self.param_grid: Dict[str, ParameterRange] = {}
        self.logger = logging.getLogger("ftseek.v4.grid_search")
        self.logger.propagate = False  # 防止日志重复

    def add_parameter(self, param_range: ParameterRange):
        """添加参数范围"""
        self.param_grid[param_range.name] = param_range
        self.logger.debug(f"添加参数：{param_range.name}")
    
    def get_combinations(self) -> List[Dict[str, float]]:
        """获取所有参数组合"""
        if not self.param_grid:
            return [{}]
        
        # 获取所有参数值
        param_names = list(self.param_grid.keys())
        param_values = [self.param_grid[name].get_values() for name in param_names]
        
        # 生成笛卡尔积
        combinations = []
        for values in itertools.product(*param_values):
            combo = dict(zip(param_names, values))
            combinations.append(combo)
        
        self.logger.info(f"生成{len(combinations)}个参数组合")
        return combinations


class ParameterOptimizer:
    """参数优化器"""
    
    def __init__(
        self,
        optimization_metric: str = "sharpe_ratio",
        cv_folds: int = 5,
        max_trials: int = 100,
        complexity_penalty: float = 0.01
    ):
        """
        初始化参数优化器
        
        Args:
            optimization_metric: 优化指标
            cv_folds: 交叉验证折数
            max_trials: 最大试验次数
            complexity_penalty: 复杂度惩罚系数
        """
        self.optimization_metric = optimization_metric
        self.cv_folds = cv_folds
        self.max_trials = max_trials
        self.complexity_penalty = complexity_penalty
        
        self.param_ranges: Dict[str, ParameterRange] = {}
        self.signals: List[Dict[str, Any]] = []
        self.best_result: Optional[OptimizationResult] = None
        
        self.logger = logging.getLogger("ftseek.v4.parameter_optimizer")
        self.logger.propagate = False  # 防止日志重复

    def add_parameter_range(
        self,
        name: str,
        min_val: float,
        max_val: float,
        step: float
    ):
        """
        添加参数范围
        
        Args:
            name: 参数名
            min_val: 最小值
            max_val: 最大值
            step: 步长
        """
        self.param_ranges[name] = ParameterRange(name, min_val, max_val, step)
        self.logger.info(f"添加参数范围：{name} [{min_val}, {max_val}] step={step}")
    
    def set_metric(self, metric: str):
        """设置优化指标"""
        self.optimization_metric = metric
        self.logger.info(f"设置优化指标：{metric}")
    
    def set_cv_folds(self, folds: int):
        """设置交叉验证折数"""
        self.cv_folds = folds
        self.logger.info(f"设置交叉验证折数：{folds}")
    
    def load_signals(self, signals: List[Dict[str, Any]]):
        """
        加载信号用于优化
        
        Args:
            signals: 信号列表
        """
        self.signals = signals
        self.logger.info(f"加载{len(signals)}个信号用于优化")
    
    def run_grid_search(
        self,
        evaluate_fn: Optional[Callable[[Dict[str, float]], float]] = None
    ) -> OptimizationResult:
        """
        运行网格搜索
        
        Args:
            evaluate_fn: 评估函数 (可选，默认使用内部评估)
            
        Returns:
            优化结果
        """
        if not self.param_ranges:
            self.logger.warning("无参数范围，返回空结果")
            return self._empty_result()
        
        # 创建网格搜索
        grid = GridSearch()
        for param_range in self.param_ranges.values():
            grid.add_parameter(param_range)
        
        combinations = grid.get_combinations()
        
        # 限制试验次数
        if len(combinations) > self.max_trials:
            self.logger.warning(f"组合数{len(combinations)}超过限制{self.max_trials}，随机采样")
            import random
            combinations = random.sample(combinations, self.max_trials)
        
        self.logger.info(f"开始网格搜索：{len(combinations)}个组合")
        
        best_score = -float('inf')
        best_params = {}
        all_trials = []
        
        for params in combinations:
            # 评估参数
            if evaluate_fn:
                score = evaluate_fn(params)
            else:
                score = self._evaluate_params(params)
            
            # 应用复杂度惩罚
            penalized_score = self._apply_complexity_penalty(score, params)
            
            trial = {
                "params": params,
                "score": score,
                "penalized_score": penalized_score,
            }
            all_trials.append(trial)
            
            if penalized_score > best_score:
                best_score = penalized_score
                best_params = params.copy()
        
        # 计算回测指标
        sharpe, max_dd = self._calculate_backtest_metrics(best_params)
        
        # 基线分数 (默认参数)
        baseline_score = self._evaluate_params({}) if self.signals else 0.0
        
        result = OptimizationResult(
            best_params=best_params,
            best_score=best_score,
            total_trials=len(combinations),
            sharpe_ratio=sharpe,
            max_drawdown=max_dd,
            baseline_score=baseline_score,
            all_trials=all_trials
        )
        
        self.best_result = result
        self.logger.info(
            f"网格搜索完成：最佳分数={best_score:.4f}, "
            f"参数={best_params}"
        )
        
        return result
    
    def _evaluate_params(self, params: Dict[str, float]) -> float:
        """
        评估参数
        
        Args:
            params: 参数字典
            
        Returns:
            评估分数
        """
        if not self.signals:
            return 0.0
        
        # 使用交叉验证
        cv_scores = self._cross_validate(params)
        
        if not cv_scores:
            return 0.0
        
        # 返回平均分数
        return sum(cv_scores) / len(cv_scores)
    
    def _cross_validate(
        self,
        params: Dict[str, float]
    ) -> List[float]:
        """
        交叉验证
        
        Args:
            params: 参数字典
            
        Returns:
            各折分数
        """
        if len(self.signals) < self.cv_folds:
            # 数据不足，直接评估
            return [self._evaluate_fold(params, self.signals)]
        
        # 分割数据
        fold_size = len(self.signals) // self.cv_folds
        scores = []
        
        for i in range(self.cv_folds):
            # 测试集
            test_start = i * fold_size
            test_end = test_start + fold_size if i < self.cv_folds - 1 else len(self.signals)
            test_signals = self.signals[test_start:test_end]
            
            # 训练集
            train_signals = self.signals[:test_start] + self.signals[test_end:]
            
            # 评估
            if train_signals:
                old_signals = self.signals
                self.signals = train_signals
                score = self._evaluate_fold(params, test_signals)
                self.signals = old_signals
                scores.append(score)
        
        return scores
    
    def _evaluate_fold(
        self,
        params: Dict[str, float],
        test_signals: List[Dict[str, Any]]
    ) -> float:
        """
        评估单折
        
        Args:
            params: 参数字典
            test_signals: 测试信号
            
        Returns:
            分数
        """
        if not test_signals:
            return 0.0
        
        # 应用参数过滤信号
        filtered_signals = self._apply_params_to_signals(params, test_signals)
        
        if not filtered_signals:
            return 0.0
        
        # 计算指标
        if self.optimization_metric == "sharpe_ratio":
            return self._calculate_sharpe(filtered_signals)
        elif self.optimization_metric == "win_rate":
            return self._calculate_win_rate(filtered_signals)
        elif self.optimization_metric == "profit_factor":
            return self._calculate_profit_factor(filtered_signals)
        else:
            return self._calculate_sharpe(filtered_signals)
    
    def _apply_params_to_signals(
        self,
        params: Dict[str, float],
        signals: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        应用参数过滤信号
        
        Args:
            params: 参数字典
            signals: 信号列表
            
        Returns:
            过滤后的信号
        """
        filtered = []
        
        for signal in signals:
            # 置信度阈值过滤
            if "confidence_threshold" in params:
                if signal.get("confidence", 1.0) < params["confidence_threshold"]:
                    continue
            
            # 止损比例过滤
            if "stop_loss_pct" in params:
                signal["stop_loss_pct"] = params["stop_loss_pct"]
            
            # 止盈比例过滤
            if "take_profit_pct" in params:
                signal["take_profit_pct"] = params["take_profit_pct"]
            
            filtered.append(signal)
        
        return filtered
    
    def _calculate_sharpe(self, signals: List[Dict[str, Any]]) -> float:
        """计算夏普比率"""
        if len(signals) < 2:
            return 0.0
        
        returns = []
        for signal in signals:
            entry = signal.get("entry_price", 0)
            exit_p = signal.get("exit_price", 0)
            if entry > 0:
                ret = (exit_p - entry) / entry
                returns.append(ret)
        
        if not returns:
            return 0.0
        
        avg_return = sum(returns) / len(returns)
        variance = sum((r - avg_return) ** 2 for r in returns) / len(returns)
        std_dev = math.sqrt(variance) if variance > 0 else 1e-6
        
        sharpe = avg_return / std_dev * math.sqrt(252)
        return sharpe
    
    def _calculate_win_rate(self, signals: List[Dict[str, Any]]) -> float:
        """计算胜率"""
        if not signals:
            return 0.0
        
        wins = sum(
            1 for s in signals
            if s.get("exit_price", 0) > s.get("entry_price", 0)
        )
        
        return wins / len(signals)
    
    def _calculate_profit_factor(self, signals: List[Dict[str, Any]]) -> float:
        """计算盈亏比"""
        gross_profit = 0.0
        gross_loss = 0.0
        
        for signal in signals:
            entry = signal.get("entry_price", 0)
            exit_p = signal.get("exit_price", 0)
            pnl = exit_p - entry
            
            if pnl > 0:
                gross_profit += pnl
            else:
                gross_loss += abs(pnl)
        
        if gross_loss == 0:
            return float('inf') if gross_profit > 0 else 0.0
        
        return gross_profit / gross_loss
    
    def _calculate_backtest_metrics(
        self,
        params: Dict[str, float]
    ) -> Tuple[float, float]:
        """计算回测指标"""
        if not self.signals:
            return 0.0, 0.0
        
        filtered = self._apply_params_to_signals(params, self.signals)
        sharpe = self._calculate_sharpe(filtered)
        
        # 简化回撤计算
        max_dd = 0.15  # 默认
        
        return sharpe, max_dd
    
    def _apply_complexity_penalty(
        self,
        score: float,
        params: Dict[str, float]
    ) -> float:
        """
        应用复杂度惩罚 (防止过拟合)
        
        Args:
            score: 原始分数
            params: 参数
            
        Returns:
            惩罚后分数
        """
        num_params = len(params)
        
        # 参数越多，惩罚越大
        penalty = self.complexity_penalty * num_params
        
        penalized_score = score - penalty
        
        self.logger.debug(
            f"复杂度惩罚：{num_params}个参数，惩罚={penalty:.4f}, "
            f"原始={score:.4f}, 惩罚后={penalized_score:.4f}"
        )
        
        return penalized_score
    
    def _empty_result(self) -> OptimizationResult:
        """返回空结果"""
        return OptimizationResult(
            best_params={},
            best_score=0.0,
            total_trials=0,
            sharpe_ratio=0.0,
            max_drawdown=0.0
        )
    
    def analyze_sensitivity(
        self,
        param_name: str,
        evaluate_fn: Optional[Callable[[Dict[str, float]], float]] = None
    ) -> Dict[str, Any]:
        """
        分析参数敏感性
        
        Args:
            param_name: 参数名
            evaluate_fn: 评估函数
            
        Returns:
            敏感性分析结果
        """
        if param_name not in self.param_ranges:
            return {"error": f"参数{param_name}不存在"}
        
        param_range = self.param_ranges[param_name]
        values = param_range.get_values()
        
        scores = []
        for value in values:
            params = {param_name: value}
            if evaluate_fn:
                score = evaluate_fn(params)
            else:
                score = self._evaluate_params(params)
            scores.append(score)
        
        # 计算敏感性 (分数变化幅度)
        if scores:
            sensitivity = max(scores) - min(scores)
        else:
            sensitivity = 0.0
        
        return {
            "param_name": param_name,
            "values": values,
            "scores": scores,
            "sensitivity": sensitivity,
            "best_value": values[scores.index(max(scores))] if scores else None,
        }
    
    def get_best_params(self) -> Dict[str, float]:
        """获取最佳参数"""
        if self.best_result:
            return self.best_result.best_params
        return {}
    
    def generate_report(self) -> Dict[str, Any]:
        """生成优化报告"""
        if not self.best_result:
            return {"error": "无优化结果"}
        
        report = {
            "summary": self.best_result.to_dict(),
            "param_ranges": {
                name: range.to_dict()
                for name, range in self.param_ranges.items()
            },
            "config": {
                "optimization_metric": self.optimization_metric,
                "cv_folds": self.cv_folds,
                "max_trials": self.max_trials,
                "complexity_penalty": self.complexity_penalty,
            },
        }
        
        self.logger.info(f"生成优化报告：{self.best_result.total_trials}次试验")
        return report
    
    def reset(self):
        """重置优化器"""
        self.param_ranges.clear()
        self.signals.clear()
        self.best_result = None
        self.logger.info("优化器已重置")
    
    def get_status(self) -> dict:
        """获取优化器状态"""
        return {
            "num_parameters": len(self.param_ranges),
            "num_signals": len(self.signals),
            "has_result": self.best_result is not None,
            "optimization_metric": self.optimization_metric,
        }


# 全局实例 (可选)
parameter_optimizer: Optional[ParameterOptimizer] = None


def get_parameter_optimizer() -> ParameterOptimizer:
    """获取 ParameterOptimizer 单例"""
    global parameter_optimizer
    if parameter_optimizer is None:
        parameter_optimizer = ParameterOptimizer()
    return parameter_optimizer
