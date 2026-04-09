"""
过拟合检测模块

实现多种过拟合检测方法，确保策略稳健性。
"""

import numpy as np
from scipy import stats
from typing import List, Dict, Tuple, Any
import logging

logger = logging.getLogger("ftseek.autoresearch.overfitting")
logger.propagate = False


class OverfittingDetector:
    """
    过拟合检测器
    
    实现多种过拟合检测方法：
    1. 性能差异测试
    2. 参数敏感性测试
    3. 蒙特卡洛模拟
    4. 防御性夏普比率
    """
    
    def __init__(self, train_results, test_results):
        """
        初始化过拟合检测器
        
        Args:
            train_results: 训练集回测结果
            test_results: 测试集回测结果
        """
        self.train = train_results
        self.test = test_results
        self.logger = logging.getLogger("ftseek.autoresearch.overfitting")
    
    def detect_all(self) -> Dict[str, float]:
        """
        执行所有检测方法
        
        Returns:
            检测结果字典
        """
        return {
            'performance_gap': self.performance_gap_test(),
            'parameter_sensitivity': self.parameter_sensitivity_test(),
            'monte_carlo': self.monte_carlo_test(),
            'deflated_sharpe': self.deflated_sharpe_ratio(),
            'overall_score': self.overall_score(),
        }
    
    def performance_gap_test(self) -> float:
        """
        性能差异测试
        
        计算训练集和测试集的夏普比率差异。
        差异 > 30% 认为有过拟合风险。
        
        Returns:
            性能差异比率 (0-1, 越小越好)
        """
        if self.train.sharpe_ratio <= 0:
            return 1.0
        
        gap = abs(self.train.sharpe_ratio - self.test.sharpe_ratio)
        ratio = gap / abs(self.train.sharpe_ratio)
        
        self.logger.info(
            f"Performance gap: train={self.train.sharpe_ratio:.2f}, "
            f"test={self.test.sharpe_ratio:.2f}, gap={ratio:.2%}"
        )
        
        return ratio
    
    def parameter_sensitivity_test(self, strategy, base_params: Dict[str, Any],
                                   perturbations: List[float] = None) -> float:
        """
        参数敏感性测试
        
        测试参数微小变化对性能的影响。
        敏感性 > 2.0 认为参数过于敏感。
        
        Args:
            strategy: 策略实例
            base_params: 基础参数
            perturbations: 扰动列表
        
        Returns:
            平均敏感性 (越小越好)
        """
        if perturbations is None:
            perturbations = [0.05, 0.10, 0.15]
        
        sensitivities = []
        
        for param_name in base_params:
            base_value = base_params[param_name]
            
            # 跳过非数值参数
            if not isinstance(base_value, (int, float)):
                continue
            
            try:
                base_result = strategy.run(base_params)
            except Exception:
                continue
            
            for perturbation in perturbations:
                # 参数扰动
                perturbed_params = base_params.copy()
                perturbed_params[param_name] = base_value * (1 + perturbation)
                
                try:
                    perturbed_result = strategy.run(perturbed_params)
                except Exception:
                    continue
                
                # 计算性能变化率
                if base_result.sharpe_ratio != 0:
                    sensitivity = abs(
                        perturbed_result.sharpe_ratio - base_result.sharpe_ratio
                    ) / perturbation
                    sensitivities.append(sensitivity)
        
        # 平均敏感性
        avg_sensitivity = np.mean(sensitivities) if sensitivities else 0
        
        self.logger.info(f"Parameter sensitivity: {avg_sensitivity:.2f}")
        
        return avg_sensitivity
    
    def monte_carlo_test(self, trades: List[float], n_simulations: int = 1000) -> float:
        """
        蒙特卡洛模拟
        
        随机打乱交易顺序，检验策略稳定性。
        原始收益在模拟分布的 5-95 分位数内认为稳定。
        
        Args:
            trades: 交易收益列表
            n_simulations: 模拟次数
        
        Returns:
            稳定性评分 (0-1, 越大越好)
        """
        if not trades or len(trades) < 10:
            return 0.0
        
        # 随机打乱交易顺序
        simulated_returns = []
        
        for _ in range(n_simulations):
            shuffled_trades = np.random.permutation(trades)
            simulated_return = np.sum(shuffled_trades)
            simulated_returns.append(simulated_return)
        
        # 计算原始收益在模拟分布中的位置
        original_return = np.sum(trades)
        percentile = stats.percentileofscore(simulated_returns, original_return)
        
        # 稳定性评分
        stability_score = min(percentile, 100 - percentile) / 50.0
        
        self.logger.info(
            f"Monte Carlo: original={original_return:.2f}, "
            f"percentile={percentile:.1f}, stability={stability_score:.2f}"
        )
        
        return stability_score
    
    def deflated_sharpe_ratio(self, n_trials: int = 100) -> float:
        """
        防御性夏普比率
        
        考虑策略尝试次数，调整夏普比率。
        
        Args:
            n_trials: 假设的策略尝试次数
        
        Returns:
            防御性夏普比率
        """
        # 计算防御性夏普比率
        dsr = self.test.sharpe_ratio * (1 - (1 - 0.5) ** (1 / n_trials))
        
        self.logger.info(f"Deflated Sharpe: {dsr:.2f} (trials={n_trials})")
        
        return dsr
    
    def overall_score(self) -> float:
        """
        综合过拟合评分
        
        综合所有检测方法，给出总体评分。
        
        Returns:
            综合评分 (0-1, 越小越好)
        """
        # 性能差异权重 40%
        performance_gap = self.performance_gap_test()
        
        # 蒙特卡洛稳定性权重 30% (转换为过拟合评分)
        monte_carlo_stability = self.monte_carlo_test([])
        monte_carlo_score = 1 - monte_carlo_stability
        
        # 防御性夏普比率权重 30% (转换为过拟合评分)
        dsr = self.deflated_sharpe_ratio()
        dsr_score = max(0, 1 - dsr / self.train.sharpe_ratio) if self.train.sharpe_ratio > 0 else 1
        
        # 综合评分
        overall = (
            performance_gap * 0.4 +
            monte_carlo_score * 0.3 +
            dsr_score * 0.3
        )
        
        self.logger.info(f"Overall overfitting score: {overall:.2f}")
        
        return overall
    
    def is_overfitting(self, threshold: float = 0.3) -> bool:
        """
        判断是否过拟合
        
        Args:
            threshold: 过拟合评分阈值
        
        Returns:
            是否过拟合
        """
        score = self.overall_score()
        return score > threshold
    
    def get_recommendations(self) -> List[str]:
        """
        获取优化建议
        
        Returns:
            建议列表
        """
        recommendations = []
        
        # 性能差异过大
        if self.performance_gap_test() > 0.3:
            recommendations.append(
                "训练集和测试集性能差异过大，建议："
                "1. 增加训练数据量"
                "2. 减少参数数量"
                "3. 使用更简单的策略"
            )
        
        # 参数敏感性过高
        # if self.parameter_sensitivity_test(...) > 2.0:
        #     recommendations.append(
        #         "参数敏感性过高，建议："
        #         "1. 使用更稳健的参数范围"
        #         "2. 增加参数约束"
        #     )
        
        # 蒙特卡洛稳定性差
        # if self.monte_carlo_test(...) < 0.5:
        #     recommendations.append(
        #         "策略稳定性差，建议："
        #         "1. 增加止损机制"
        #         "2. 降低交易频率"
        #     )
        
        return recommendations if recommendations else ["策略稳健性良好"]
