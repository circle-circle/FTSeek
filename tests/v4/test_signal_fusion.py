"""
SignalFusion 单元测试

测试多因子信号融合系统，整合：
- PreMarketAnalyzer (盘前量比分析)
- MomentumScanner (动量催化扫描)
- PatternRecognizer (技术形态识别)
- CatalystTracker (催化事件追踪)

测试用例:
1. test_score_calculation - 各维度评分计算
2. test_fusion_formula - 融合公式验证
3. test_decision_thresholds - 决策阈值测试
4. test_signal_generation - 信号生成测试
5. test_multi_symbol_scan - 多标的扫描测试
6. test_weight_configuration - 权重配置测试
7. test_report_generation - 报告生成测试
"""

import pytest
import time
from typing import Dict, List

from app.v4.strategy.signal_fusion import SignalFusion, FusionSignal, FusionReport


class TestScoreCalculation:
    """测试各维度评分计算"""

    def test_score_calculation(self):
        """测试各维度评分计算"""
        fusion = SignalFusion()
        
        # 准备测试数据
        symbol = "US.AAPL"
        market_data = {
            'price': 150.0,
            'volume': 1000000,
            'premarket_volume_ratio': 2.5,
            'price_momentum': 75.0,
            'volume_momentum': 80.0,
            'catalyst_strength': 85.0,
            'klines': [
                {'time': i, 'open': 148 + i * 0.1, 'high': 150 + i * 0.1, 
                 'low': 147 + i * 0.1, 'close': 149 + i * 0.1, 'volume': 1000000}
                for i in range(60)
            ],
        }
        
        # 计算各维度评分
        scores = fusion.calculate_score(symbol, market_data)
        
        # 验证返回结构
        assert isinstance(scores, dict)
        assert 'premarket' in scores
        assert 'momentum' in scores
        assert 'pattern' in scores
        assert 'catalyst' in scores
        
        # 验证分数范围 (0-1)
        for key, value in scores.items():
            assert 0.0 <= value <= 1.0, f"{key} score {value} out of range [0, 1]"
        
        print(f"✓ 各维度评分计算正常: {scores}")


class TestFusionFormula:
    """测试融合公式验证"""

    def test_fusion_formula(self):
        """测试融合公式验证"""
        fusion = SignalFusion()
        
        # 准备测试分数
        scores = {
            'premarket': 0.8,
            'momentum': 0.7,
            'pattern': 0.9,
            'catalyst': 0.6
        }
        
        # 计算融合分数
        final_score = fusion.fuse_scores(scores)
        
        # 验证融合公式
        # final_score = premarket × 0.20 + momentum × 0.25 + pattern × 0.30 + catalyst × 0.25
        expected_score = (
            0.8 * 0.20 +  # 0.16
            0.7 * 0.25 +  # 0.175
            0.9 * 0.30 +  # 0.27
            0.6 * 0.25    # 0.15
        )  # 0.755
        
        assert abs(final_score - expected_score) < 0.001, \
            f"Expected {expected_score}, got {final_score}"
        
        # 验证分数范围
        assert 0.0 <= final_score <= 1.0
        
        print(f"✓ 融合公式验证通过: {final_score:.4f}")

    def test_fusion_formula_edge_cases(self):
        """测试融合公式边界情况"""
        fusion = SignalFusion()
        
        # 全零分数
        scores_zero = {'premarket': 0.0, 'momentum': 0.0, 'pattern': 0.0, 'catalyst': 0.0}
        assert fusion.fuse_scores(scores_zero) == 0.0
        
        # 全满分
        scores_full = {'premarket': 1.0, 'momentum': 1.0, 'pattern': 1.0, 'catalyst': 1.0}
        assert fusion.fuse_scores(scores_full) == 1.0
        
        print("✓ 融合公式边界情况测试通过")


class TestDecisionThresholds:
    """测试决策阈值"""

    def test_decision_thresholds(self):
        """测试决策阈值测试"""
        fusion = SignalFusion()
        
        # 测试 BUY 阈值 (score >= 0.75)
        assert fusion.make_decision(0.75) == 'BUY'
        assert fusion.make_decision(0.80) == 'BUY'
        assert fusion.make_decision(1.0) == 'BUY'
        
        # 测试 WATCH 阈值 (0.50 <= score < 0.75)
        assert fusion.make_decision(0.50) == 'WATCH'
        assert fusion.make_decision(0.60) == 'WATCH'
        assert fusion.make_decision(0.74) == 'WATCH'
        
        # 测试 SKIP 阈值 (score < 0.50)
        assert fusion.make_decision(0.49) == 'SKIP'
        assert fusion.make_decision(0.30) == 'SKIP'
        assert fusion.make_decision(0.0) == 'SKIP'
        
        print("✓ 决策阈值测试通过")

    def test_decision_boundary_values(self):
        """测试决策边界值"""
        fusion = SignalFusion()
        
        # 精确边界测试
        assert fusion.make_decision(0.749) == 'WATCH'  # 略低于 BUY 阈值
        assert fusion.make_decision(0.750) == 'BUY'    # 精确 BUY 阈值
        assert fusion.make_decision(0.499) == 'SKIP'   # 略低于 WATCH 阈值
        assert fusion.make_decision(0.500) == 'WATCH'  # 精确 WATCH 阈值
        
        print("✓ 决策边界值测试通过")


class TestSignalGeneration:
    """测试信号生成"""

    def test_signal_generation(self):
        """测试信号生成测试"""
        fusion = SignalFusion()
        
        # 准备测试数据
        symbol = "US.AAPL"
        market_data = {
            'price': 150.0,
            'volume': 1000000,
            'premarket_volume_ratio': 3.5,  # 高量比
            'price_momentum': 80.0,
            'volume_momentum': 85.0,
            'catalyst_strength': 90.0,
            'klines': [
                {'time': i, 'open': 148 + i * 0.1, 'high': 150 + i * 0.1, 
                 'low': 147 + i * 0.1, 'close': 149 + i * 0.1, 'volume': 1000000}
                for i in range(60)
            ],
        }
        
        # 生成信号
        signal = fusion.generate_signal(symbol, market_data)
        
        # 验证信号类型
        assert isinstance(signal, FusionSignal)
        
        # 验证必填字段
        assert signal.symbol == symbol
        assert signal.action in ['BUY', 'SELL', 'WATCH', 'SKIP']
        assert signal.direction in ['LONG', 'SHORT']
        assert 0.0 <= signal.confidence <= 1.0
        
        # 验证各维度分数
        assert 0.0 <= signal.premarket_score <= 1.0
        assert 0.0 <= signal.momentum_score <= 1.0
        assert 0.0 <= signal.pattern_score <= 1.0
        assert 0.0 <= signal.catalyst_score <= 1.0
        assert 0.0 <= signal.final_score <= 1.0
        
        # 验证价格相关字段
        assert signal.entry_price > 0
        assert signal.stop_loss > 0
        assert signal.target_price > 0
        assert 0.0 <= signal.position_ratio <= 1.0
        
        # 验证其他字段
        assert isinstance(signal.rationale, str)
        assert signal.generated_at > 0
        
        print(f"✓ 信号生成测试通过: {signal.symbol} -> {signal.action}")


class TestMultiSymbolScan:
    """测试多标的扫描"""

    def test_multi_symbol_scan(self):
        """测试多标的扫描测试"""
        fusion = SignalFusion()
        
        # 准备多个标的
        symbols = ["US.AAPL", "US.NVDA", "US.TSLA", "HK.00700"]
        
        # 准备市场数据
        market_data = {
            'price': 150.0,
            'volume': 1000000,
            'premarket_volume_ratio': 2.0,
            'price_momentum': 70.0,
            'volume_momentum': 75.0,
            'catalyst_strength': 80.0,
            'klines': [
                {'time': i, 'open': 148 + i * 0.1, 'high': 150 + i * 0.1, 
                 'low': 147 + i * 0.1, 'close': 149 + i * 0.1, 'volume': 1000000}
                for i in range(60)
            ],
        }
        
        # 扫描多个标的
        report = fusion.scan_multiple(symbols, market_data)
        
        # 验证报告类型
        assert isinstance(report, FusionReport)
        
        # 验证报告结构
        assert isinstance(report.signals, list)
        assert len(report.signals) == len(symbols)
        assert isinstance(report.scan_time, float)
        assert isinstance(report.market_condition, str)
        assert isinstance(report.top_picks, list)
        
        # 验证每个信号
        for signal in report.signals:
            assert isinstance(signal, FusionSignal)
            assert signal.symbol in symbols
        
        # 验证 top_picks 是 signals 的子集
        top_pick_symbols = [s.symbol for s in report.signals if s.symbol in report.top_picks]
        assert len(top_pick_symbols) <= len(report.signals)
        
        print(f"✓ 多标的扫描测试通过: 扫描 {len(symbols)} 个标的，生成 {len(report.signals)} 个信号")


class TestWeightConfiguration:
    """测试权重配置"""

    def test_weight_configuration(self):
        """测试权重配置测试"""
        # 使用默认权重
        fusion_default = SignalFusion()
        assert fusion_default.weights == {
            'premarket': 0.20,
            'momentum': 0.25,
            'pattern': 0.30,
            'catalyst': 0.25
        }
        
        # 使用自定义权重
        custom_weights = {
            'premarket': 0.30,
            'momentum': 0.30,
            'pattern': 0.20,
            'catalyst': 0.20
        }
        fusion_custom = SignalFusion(config={'weights': custom_weights})
        assert fusion_custom.weights == custom_weights
        
        print("✓ 权重配置测试通过")

    def test_weight_sum_validation(self):
        """测试权重总和验证"""
        # 权重总和应该为 1.0
        fusion = SignalFusion()
        weight_sum = sum(fusion.weights.values())
        assert abs(weight_sum - 1.0) < 0.001, f"权重总和 {weight_sum} 不等于 1.0"
        
        print("✓ 权重总和验证通过")


class TestReportGeneration:
    """测试报告生成"""

    def test_report_generation(self):
        """测试报告生成测试"""
        fusion = SignalFusion()
        
        # 准备多个标的
        symbols = ["US.AAPL", "US.NVDA", "US.TSLA"]
        
        # 准备市场数据
        market_data = {
            'price': 150.0,
            'volume': 1000000,
            'premarket_volume_ratio': 2.5,
            'price_momentum': 75.0,
            'volume_momentum': 80.0,
            'catalyst_strength': 85.0,
            'klines': [
                {'time': i, 'open': 148 + i * 0.1, 'high': 150 + i * 0.1, 
                 'low': 147 + i * 0.1, 'close': 149 + i * 0.1, 'volume': 1000000}
                for i in range(60)
            ],
        }
        
        # 生成报告
        report = fusion.scan_multiple(symbols, market_data)
        
        # 验证报告内容
        assert isinstance(report, FusionReport)
        assert len(report.signals) > 0
        assert report.scan_time > 0
        assert report.market_condition in ['bullish', 'neutral', 'bearish']
        
        # 验证 top_picks 不为空
        assert len(report.top_picks) > 0
        assert len(report.top_picks) <= len(report.signals)
        
        # 验证 top_picks 基于 final_score 排序
        if len(report.signals) > 1:
            sorted_signals = sorted(report.signals, key=lambda s: s.final_score, reverse=True)
            expected_top_picks = [s.symbol for s in sorted_signals[:len(report.top_picks)]]
            assert report.top_picks == expected_top_picks, \
                f"top_picks 应该按 final_score 降序排列"
        
        print(f"✓ 报告生成测试通过: 市场条件={report.market_condition}, top_picks={report.top_picks}")


class TestIntegration:
    """集成测试"""

    def test_full_workflow(self):
        """测试完整工作流程"""
        fusion = SignalFusion()
        
        # 准备测试数据
        symbol = "US.AAPL"
        market_data = {
            'price': 150.0,
            'volume': 1000000,
            'premarket_volume_ratio': 3.0,
            'price_momentum': 80.0,
            'volume_momentum': 85.0,
            'catalyst_strength': 90.0,
            'klines': [
                {'time': i, 'open': 148 + i * 0.1, 'high': 150 + i * 0.1, 
                 'low': 147 + i * 0.1, 'close': 149 + i * 0.1, 'volume': 1000000}
                for i in range(60)
            ],
        }
        
        # 1. 计算各维度评分
        scores = fusion.calculate_score(symbol, market_data)
        assert isinstance(scores, dict)
        
        # 2. 融合评分
        final_score = fusion.fuse_scores(scores)
        assert 0.0 <= final_score <= 1.0
        
        # 3. 生成决策
        decision = fusion.make_decision(final_score)
        assert decision in ['BUY', 'WATCH', 'SKIP']
        
        # 4. 生成完整信号
        signal = fusion.generate_signal(symbol, market_data)
        assert signal.action == decision
        
        # 5. 多标的扫描
        symbols = ["US.AAPL", "US.NVDA"]
        report = fusion.scan_multiple(symbols, market_data)
        assert len(report.signals) == 2
        
        print("✓ 完整工作流程测试通过")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
