"""
ConfidenceScorer 单元测试

测试置信度评分系统的核心功能：
- 权重计算
- 分数边界
- 级别分类
- 仓位比例
- 因子验证
- 自定义权重
- 完整结果生成
"""

import pytest
from app.v4.strategy.confidence_scorer import (
    ConfidenceScorer,
    ConfidenceFactors,
    ConfidenceResult,
)


class TestWeightCalculation:
    """测试权重计算功能"""

    def test_weight_calculation(self):
        """测试默认权重下的分数计算"""
        scorer = ConfidenceScorer()
        factors = ConfidenceFactors(
            technical=1.0,  # 100%
            catalyst=1.0,   # 100%
            sentiment=1.0,  # 100%
            volume=1.0      # 100%
        )
        score = scorer.calculate(factors)
        # 1.0 * 0.4 + 1.0 * 0.3 + 1.0 * 0.2 + 1.0 * 0.1 = 1.0
        assert abs(score - 1.0) < 0.001, "所有因子为 1.0 时，综合分数应为 1.0"

    def test_weight_calculation_mixed(self):
        """测试混合分数下的权重计算"""
        scorer = ConfidenceScorer()
        factors = ConfidenceFactors(
            technical=0.8,  # 0.8 * 0.4 = 0.32
            catalyst=0.5,   # 0.5 * 0.3 = 0.15
            sentiment=0.6,  # 0.6 * 0.2 = 0.12
            volume=0.4      # 0.4 * 0.1 = 0.04
        )
        score = scorer.calculate(factors)
        expected = 0.32 + 0.15 + 0.12 + 0.04  # 0.63
        assert abs(score - expected) < 0.001, f"混合分数计算错误，期望 {expected}, 得到 {score}"


class TestScoreBoundaries:
    """测试分数边界"""

    def test_score_boundaries(self):
        """测试分数是否在 0-1 范围内"""
        scorer = ConfidenceScorer()

        # 测试最小值
        factors_min = ConfidenceFactors(
            technical=0.0,
            catalyst=0.0,
            sentiment=0.0,
            volume=0.0
        )
        score_min = scorer.calculate(factors_min)
        assert score_min == 0.0, "最小分数应为 0.0"

        # 测试最大值
        factors_max = ConfidenceFactors(
            technical=1.0,
            catalyst=1.0,
            sentiment=1.0,
            volume=1.0
        )
        score_max = scorer.calculate(factors_max)
        assert abs(score_max - 1.0) < 0.001, "最大分数应为 1.0"

    def test_score_boundary_clamping(self):
        """测试超出边界的分数是否被正确限制"""
        scorer = ConfidenceScorer()

        # 虽然输入应该被验证，但计算结果不应超出 0-1
        factors = ConfidenceFactors(
            technical=0.5,
            catalyst=0.5,
            sentiment=0.5,
            volume=0.5
        )
        score = scorer.calculate(factors)
        assert 0.0 <= score <= 1.0, f"分数 {score} 超出 0-1 范围"


class TestLevelClassification:
    """测试置信度级别分类"""

    def test_level_classification(self):
        """测试各级别的分数阈值"""
        scorer = ConfidenceScorer()

        # STRONG: score >= 0.80
        assert scorer.get_level(0.80) == "STRONG"
        assert scorer.get_level(0.85) == "STRONG"
        assert scorer.get_level(0.95) == "STRONG"
        assert scorer.get_level(1.0) == "STRONG"

        # RECOMMENDED: 0.60 <= score < 0.80
        assert scorer.get_level(0.60) == "RECOMMENDED"
        assert scorer.get_level(0.65) == "RECOMMENDED"
        assert scorer.get_level(0.79) == "RECOMMENDED"

        # WATCH: 0.40 <= score < 0.60
        assert scorer.get_level(0.40) == "WATCH"
        assert scorer.get_level(0.45) == "WATCH"
        assert scorer.get_level(0.59) == "WATCH"

        # SKIP: score < 0.40
        assert scorer.get_level(0.39) == "SKIP"
        assert scorer.get_level(0.20) == "SKIP"
        assert scorer.get_level(0.0) == "SKIP"

    def test_level_boundary_conditions(self):
        """测试级别边界的精确条件"""
        scorer = ConfidenceScorer()

        # 边界值测试
        assert scorer.get_level(0.80) == "STRONG", "0.80 应该是 STRONG"
        assert scorer.get_level(0.799) == "RECOMMENDED", "0.799 应该是 RECOMMENDED"
        assert scorer.get_level(0.60) == "RECOMMENDED", "0.60 应该是 RECOMMENDED"
        assert scorer.get_level(0.599) == "WATCH", "0.599 应该是 WATCH"
        assert scorer.get_level(0.40) == "WATCH", "0.40 应该是 WATCH"
        assert scorer.get_level(0.399) == "SKIP", "0.399 应该是 SKIP"


class TestPositionRatio:
    """测试仓位比例"""

    def test_position_ratio(self):
        """测试各级别对应的仓位比例"""
        scorer = ConfidenceScorer()

        # STRONG: 100%
        assert scorer.get_position_ratio("STRONG") == 1.0

        # RECOMMENDED: 70%
        assert scorer.get_position_ratio("RECOMMENDED") == 0.7

        # WATCH: 30%
        assert scorer.get_position_ratio("WATCH") == 0.3

        # SKIP: 0%
        assert scorer.get_position_ratio("SKIP") == 0.0

    def test_position_ratio_invalid_level(self):
        """测试无效级别的处理"""
        scorer = ConfidenceScorer()

        # 无效级别应返回 0 或抛出异常
        with pytest.raises(ValueError):
            scorer.get_position_ratio("INVALID")


class TestFactorValidation:
    """测试因子验证"""

    def test_factor_validation(self):
        """测试因子值的有效性验证"""
        scorer = ConfidenceScorer()

        # 有效因子
        valid_factors = ConfidenceFactors(
            technical=0.5,
            catalyst=0.5,
            sentiment=0.5,
            volume=0.5
        )
        score = scorer.calculate(valid_factors)
        assert 0.0 <= score <= 1.0

    def test_factor_validation_out_of_range(self):
        """测试超出范围的因子值"""
        # 因子应该在 0-1 范围内
        # 如果传入超出范围的值，应该被处理或抛出异常
        scorer = ConfidenceScorer()

        # 负值测试
        with pytest.raises(ValueError):
            factors = ConfidenceFactors(
                technical=-0.1,
                catalyst=0.5,
                sentiment=0.5,
                volume=0.5
            )
            scorer.calculate(factors)

        # 超过 1 的值测试
        with pytest.raises(ValueError):
            factors = ConfidenceFactors(
                technical=1.5,
                catalyst=0.5,
                sentiment=0.5,
                volume=0.5
            )
            scorer.calculate(factors)


class TestCustomWeights:
    """测试自定义权重"""

    def test_custom_weights(self):
        """测试自定义权重配置"""
        custom_weights = {
            'technical': 0.5,
            'catalyst': 0.3,
            'sentiment': 0.1,
            'volume': 0.1
        }
        scorer = ConfidenceScorer(weights=custom_weights)

        factors = ConfidenceFactors(
            technical=1.0,
            catalyst=0.0,
            sentiment=0.0,
            volume=0.0
        )
        score = scorer.calculate(factors)
        # 1.0 * 0.5 + 0 + 0 + 0 = 0.5
        assert abs(score - 0.5) < 0.001, f"自定义权重计算错误，期望 0.5, 得到 {score}"

    def test_custom_weights_sum_to_one(self):
        """测试自定义权重总和为 1"""
        custom_weights = {
            'technical': 0.25,
            'catalyst': 0.25,
            'sentiment': 0.25,
            'volume': 0.25
        }
        scorer = ConfidenceScorer(weights=custom_weights)

        factors = ConfidenceFactors(
            technical=1.0,
            catalyst=1.0,
            sentiment=1.0,
            volume=1.0
        )
        score = scorer.calculate(factors)
        # 所有因子为 1.0，权重和为 1.0，结果应为 1.0
        assert abs(score - 1.0) < 0.001

    def test_default_weights(self):
        """测试默认权重配置"""
        scorer = ConfidenceScorer()

        assert scorer.weights['technical'] == 0.4
        assert scorer.weights['catalyst'] == 0.3
        assert scorer.weights['sentiment'] == 0.2
        assert scorer.weights['volume'] == 0.1


class TestResultGeneration:
    """测试完整结果生成"""

    def test_result_generation(self):
        """测试完整评分结果生成"""
        scorer = ConfidenceScorer()

        factors = ConfidenceFactors(
            technical=0.9,
            catalyst=0.8,
            sentiment=0.7,
            volume=0.6
        )
        result = scorer.generate_result(factors)

        # 验证结果类型
        assert isinstance(result, ConfidenceResult)

        # 验证分数计算
        expected_score = 0.9 * 0.4 + 0.8 * 0.3 + 0.7 * 0.2 + 0.6 * 0.1
        expected_score = 0.36 + 0.24 + 0.14 + 0.06  # 0.80
        assert abs(result.score - expected_score) < 0.001

        # 验证级别
        assert result.level == "STRONG"  # 0.80 >= 0.80

        # 验证 breakdown 包含所有维度
        assert 'technical' in result.breakdown
        assert 'catalyst' in result.breakdown
        assert 'sentiment' in result.breakdown
        assert 'volume' in result.breakdown

        # 验证 recommendation 存在
        assert isinstance(result.recommendation, str)
        assert len(result.recommendation) > 0

    def test_result_generation_skip_level(self):
        """测试 SKIP 级别的结果生成"""
        scorer = ConfidenceScorer()

        factors = ConfidenceFactors(
            technical=0.2,
            catalyst=0.1,
            sentiment=0.2,
            volume=0.1
        )
        result = scorer.generate_result(factors)

        assert result.level == "SKIP"
        assert result.score < 0.40
        assert scorer.get_position_ratio(result.level) == 0.0

    def test_result_generation_breakdown_values(self):
        """测试结果中 breakdown 的值正确"""
        scorer = ConfidenceScorer()

        factors = ConfidenceFactors(
            technical=0.85,
            catalyst=0.70,
            sentiment=0.60,
            volume=0.50
        )
        result = scorer.generate_result(factors)

        assert result.breakdown['technical'] == 0.85
        assert result.breakdown['catalyst'] == 0.70
        assert result.breakdown['sentiment'] == 0.60
        assert result.breakdown['volume'] == 0.50


class TestEdgeCases:
    """测试边界情况"""

    def test_zero_factors(self):
        """测试所有因子为 0 的情况"""
        scorer = ConfidenceScorer()
        factors = ConfidenceFactors(
            technical=0.0,
            catalyst=0.0,
            sentiment=0.0,
            volume=0.0
        )
        result = scorer.generate_result(factors)

        assert result.score == 0.0
        assert result.level == "SKIP"
        assert scorer.get_position_ratio(result.level) == 0.0

    def test_perfect_factors(self):
        """测试所有因子为 1.0 的情况"""
        scorer = ConfidenceScorer()
        factors = ConfidenceFactors(
            technical=1.0,
            catalyst=1.0,
            sentiment=1.0,
            volume=1.0
        )
        result = scorer.generate_result(factors)

        assert abs(result.score - 1.0) < 0.001
        assert result.level == "STRONG"
        assert scorer.get_position_ratio(result.level) == 1.0

    def test_recommendation_content(self):
        """测试建议内容包含级别信息"""
        scorer = ConfidenceScorer()

        # 测试不同级别的建议
        strong_factors = ConfidenceFactors(1.0, 1.0, 1.0, 1.0)
        strong_result = scorer.generate_result(strong_factors)
        # 中文建议应包含"强烈建议"或"高置信度"等关键词
        assert "强烈" in strong_result.recommendation or "高" in strong_result.recommendation

        skip_factors = ConfidenceFactors(0.0, 0.0, 0.0, 0.0)
        skip_result = scorer.generate_result(skip_factors)
        # 中文建议应包含"跳过"或"不建议"等关键词
        assert "跳过" in skip_result.recommendation or "不" in skip_result.recommendation
