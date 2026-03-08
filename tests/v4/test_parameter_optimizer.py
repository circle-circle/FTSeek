"""
ParameterOptimizer 单元测试 - 参数优化模块

测试覆盖:
- 网格搜索
- 交叉验证
- 避免过拟合
- 参数范围优化
"""
import pytest
import sys
import os

# 添加项目路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from app.v4.optimization.parameter_optimizer import (
    ParameterOptimizer,
    OptimizationResult,
    GridSearch,
    ParameterRange
)


class TestParameterRange:
    """ParameterRange 数据类测试"""
    
    def test_range_creation(self):
        """测试参数范围创建"""
        param_range = ParameterRange(
            name="confidence_threshold",
            min_val=0.5,
            max_val=0.9,
            step=0.1
        )
        
        assert param_range.name == "confidence_threshold"
        assert param_range.min_val == 0.5
        assert param_range.max_val == 0.9
        assert param_range.step == 0.1
    
    def test_range_values(self):
        """测试参数值生成"""
        param_range = ParameterRange(
            name="stop_loss_pct",
            min_val=0.02,
            max_val=0.05,
            step=0.01
        )
        
        values = param_range.get_values()
        
        assert len(values) == 4  # 0.02, 0.03, 0.04, 0.05
        assert 0.02 in values
        assert 0.05 in values
    
    def test_range_to_dict(self):
        """测试参数范围转字典"""
        param_range = ParameterRange(
            name="test_param",
            min_val=0.0,
            max_val=1.0,
            step=0.1
        )
        
        param_dict = param_range.to_dict()
        
        assert param_dict["name"] == "test_param"
        assert param_dict["min_val"] == 0.0
        assert param_dict["max_val"] == 1.0


class TestOptimizationResult:
    """OptimizationResult 数据类测试"""
    
    def test_result_creation(self):
        """测试优化结果创建"""
        result = OptimizationResult(
            best_params={"confidence_threshold": 0.75, "stop_loss_pct": 0.03},
            best_score=0.65,
            total_trials=100,
            sharpe_ratio=1.5,
            max_drawdown=0.15
        )
        
        assert result.best_params["confidence_threshold"] == 0.75
        assert result.best_score == 0.65
        assert result.total_trials == 100
    
    def test_result_improvement(self):
        """测试改进计算"""
        result = OptimizationResult(
            best_params={"confidence_threshold": 0.75},
            best_score=0.65,
            total_trials=100,
            sharpe_ratio=1.5,
            max_drawdown=0.15,
            baseline_score=0.55
        )
        
        # 改进 = (0.65 - 0.55) / 0.55 = 18.18%
        improvement = result.get_improvement()
        
        assert abs(improvement - 0.1818) < 0.01


class TestGridSearch:
    """GridSearch 测试"""
    
    def test_grid_search_creation(self):
        """测试网格搜索创建"""
        grid = GridSearch()
        
        assert grid.param_grid == {}
    
    def test_add_parameter(self):
        """测试添加参数"""
        grid = GridSearch()
        
        param_range = ParameterRange(
            name="confidence_threshold",
            min_val=0.5,
            max_val=0.8,
            step=0.1
        )
        
        grid.add_parameter(param_range)
        
        assert "confidence_threshold" in grid.param_grid
        assert len(grid.param_grid) == 1
    
    def test_get_combinations(self):
        """测试参数组合生成"""
        grid = GridSearch()
        
        grid.add_parameter(ParameterRange("param1", 0.5, 0.7, 0.1))
        grid.add_parameter(ParameterRange("param2", 0.02, 0.03, 0.01))
        
        combinations = grid.get_combinations()
        
        # 3 * 2 = 6 种组合
        assert len(combinations) == 6
    
    def test_empty_grid(self):
        """测试空网格"""
        grid = GridSearch()
        
        combinations = grid.get_combinations()
        
        assert len(combinations) == 1  # 空组合
        assert combinations[0] == {}


class TestParameterOptimizer:
    """ParameterOptimizer 核心功能测试"""
    
    def test_optimizer_initialization(self):
        """测试优化器初始化"""
        optimizer = ParameterOptimizer()
        
        assert optimizer.optimization_metric == "sharpe_ratio"
        assert optimizer.cv_folds == 5
        assert optimizer.max_trials == 100
    
    def test_add_parameter_range(self):
        """测试添加参数范围"""
        optimizer = ParameterOptimizer()
        
        optimizer.add_parameter_range(
            "confidence_threshold",
            min_val=0.5,
            max_val=0.8,
            step=0.1
        )
        
        assert "confidence_threshold" in optimizer.param_ranges
    
    def test_set_optimization_metric(self):
        """测试设置优化指标"""
        optimizer = ParameterOptimizer()
        
        optimizer.set_metric("win_rate")
        
        assert optimizer.optimization_metric == "win_rate"
    
    def test_set_cv_folds(self):
        """测试设置交叉验证折数"""
        optimizer = ParameterOptimizer()
        
        optimizer.set_cv_folds(10)
        
        assert optimizer.cv_folds == 10
    
    def test_run_grid_search(self):
        """测试运行网格搜索"""
        optimizer = ParameterOptimizer()
        
        optimizer.add_parameter_range("param1", 0.5, 0.7, 0.1)
        optimizer.add_parameter_range("param2", 0.02, 0.03, 0.01)
        
        # Mock 评估函数
        def mock_evaluate(params):
            return 0.5 + params.get("param1", 0) * 0.1
        
        result = optimizer.run_grid_search(mock_evaluate)
        
        assert result is not None
        assert "best_params" in result.to_dict()
    
    def test_evaluate_params(self):
        """测试参数评估"""
        optimizer = ParameterOptimizer()
        
        params = {"confidence_threshold": 0.75, "stop_loss_pct": 0.03}
        
        # Mock 信号和回测
        optimizer.signals = [
            {"code": "HK.00700", "action": "BUY", "entry_price": 100.0, 
             "exit_price": 105.0, "quantity": 100, "confidence": 0.8}
        ]
        
        score = optimizer._evaluate_params(params)
        
        assert score is not None
    
    def test_cross_validation(self):
        """测试交叉验证"""
        optimizer = ParameterOptimizer(cv_folds=3)
        
        # Mock 信号
        optimizer.signals = [
            {"code": f"HK.00{i}", "action": "BUY", "entry_price": 100.0, 
             "exit_price": 100.0 + i, "quantity": 100}
            for i in range(10)
        ]
        
        params = {"confidence_threshold": 0.7}
        scores = optimizer._cross_validate(params)
        
        assert len(scores) == 3  # 3 折
        assert all(isinstance(s, (int, float)) for s in scores)
    
    def test_prevent_overfitting(self):
        """测试防止过拟合"""
        optimizer = ParameterOptimizer()
        
        # 过于复杂的参数组合应被惩罚
        complex_params = {
            "param1": 0.5,
            "param2": 0.03,
            "param3": 0.1,
            "param4": 0.2,
            "param5": 0.05
        }
        
        base_score = 0.70
        penalized_score = optimizer._apply_complexity_penalty(base_score, complex_params)
        
        # 复杂参数应有惩罚
        assert penalized_score < base_score
    
    def test_simple_params_no_penalty(self):
        """测试简单参数无惩罚"""
        optimizer = ParameterOptimizer(complexity_penalty=0.001)  # 降低惩罚系数
        
        simple_params = {
            "param1": 0.5,
            "param2": 0.03
        }
        
        base_score = 0.70
        penalized_score = optimizer._apply_complexity_penalty(base_score, simple_params)
        
        # 简单参数惩罚应较小 (2 个参数 * 0.001 = 0.002)
        assert abs(penalized_score - base_score) < 0.01
    
    def test_get_best_params(self):
        """测试获取最佳参数"""
        optimizer = ParameterOptimizer()
        
        # Mock 结果
        optimizer.best_result = OptimizationResult(
            best_params={"confidence_threshold": 0.75},
            best_score=0.65,
            total_trials=50,
            sharpe_ratio=1.5,
            max_drawdown=0.15
        )
        
        best_params = optimizer.get_best_params()
        
        assert best_params["confidence_threshold"] == 0.75
    
    def test_optimization_report(self):
        """测试优化报告"""
        optimizer = ParameterOptimizer()
        
        optimizer.best_result = OptimizationResult(
            best_params={"confidence_threshold": 0.75, "stop_loss_pct": 0.03},
            best_score=0.65,
            total_trials=100,
            sharpe_ratio=1.5,
            max_drawdown=0.15,
            baseline_score=0.55
        )
        
        report = optimizer.generate_report()
        
        # 报告结构检查
        assert "summary" in report
        assert "param_ranges" in report
        assert "config" in report
        assert "best_params" in report["summary"]
        assert "best_score" in report["summary"]
    
    def test_reset_optimizer(self):
        """测试重置优化器"""
        optimizer = ParameterOptimizer()
        
        optimizer.add_parameter_range("param1", 0.5, 0.7, 0.1)
        optimizer.best_result = OptimizationResult(
            best_params={},
            best_score=0.0,
            total_trials=0,
            sharpe_ratio=0.0,
            max_drawdown=0.0
        )
        
        optimizer.reset()
        
        assert len(optimizer.param_ranges) == 0
        assert optimizer.best_result is None
    
    def test_parameter_sensitivity(self):
        """测试参数敏感性分析"""
        optimizer = ParameterOptimizer()
        
        optimizer.add_parameter_range("param1", 0.5, 0.7, 0.1)
        
        # Mock 评估
        def mock_evaluate(params):
            return 0.5 + params.get("param1", 0) * 0.2
        
        sensitivity = optimizer.analyze_sensitivity("param1", mock_evaluate)
        
        assert sensitivity is not None
        assert "param_name" in sensitivity


# 运行测试
if __name__ == "__main__":
    pytest.main([__file__, "-v"])
