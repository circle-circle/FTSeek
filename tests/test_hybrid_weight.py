#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
混合权重架构测试 (Hybrid Weight Architecture Tests)

测试 WeightCalculator + WeightEngine 混合集成

2026-04-09: 混合架构实施
- WeightCalculator: 基于策略胜率/市场状态的权重
- WeightEngine: 基于新闻因子/HK-US 相关性的权重调整
- ConsensusEngine: 整合两者输出
"""
import sys
import unittest
from datetime import datetime

sys.path.insert(0, '/opt/ftseek/FTSeek')

from app.strategy.consensus_engine import (
    ConsensusEngine,
    HYBRID_WEIGHT_ENABLED,
    TREND_STRATEGIES,
    OSCILLATOR_STRATEGIES,
    SENTIMENT_STRATEGIES,
    MTF_STRATEGIES,
)


class TestHybridWeightArchitecture(unittest.TestCase):
    """混合权重架构测试"""

    def setUp(self):
        """初始化测试环境"""
        self.engine = ConsensusEngine()

    # ============================================================
    # 测试 1: WeightEngine 集成
    # ============================================================
    def test_weight_engine_integration(self):
        """测试 WeightEngine 集成状态"""
        if HYBRID_WEIGHT_ENABLED:
            self.assertIsNotNone(
                self.engine._weight_engine,
                "WeightEngine 应该可用（混合架构启用时）"
            )

    def test_weight_engine_config(self):
        """测试 WeightEngine 配置"""
        if not self.engine._weight_engine:
            self.skipTest("WeightEngine 未启用")

        config = self.engine._weight_engine.config

        # 新闻权重配置
        self.assertEqual(config.news_max_weight, 0.40, "新闻最大权重应为 40%")
        self.assertEqual(config.news_normal_weight, 0.25, "新闻一般权重应为 25%")

        # HK/US 权重配置
        self.assertEqual(config.hk_us_max_weight, 0.35, "HK/US 最大权重应为 35%")

        # 技术权重配置
        self.assertEqual(config.technical_min_weight, 0.25, "技术最小权重应为 25%")

        # 相关性阈值
        self.assertEqual(config.correlation_threshold, 0.6, "相关性阈值应为 0.6")

    # ============================================================
    # 测试 2: HK/US 映射
    # ============================================================
    def test_hk_us_mapping(self):
        """测试港股美股映射"""
        if not self.engine._weight_engine:
            self.skipTest("WeightEngine 未启用")

        # 腾讯控股
        pair = self.engine._weight_engine.get_hk_us_pair('00700.HK')
        self.assertIsNotNone(pair, "腾讯控股应该有美股配对")
        self.assertEqual(pair['us_stock'], 'TCEHY')
        self.assertGreaterEqual(pair.get('correlation', 0), 0.6)

        # 阿里巴巴
        pair = self.engine._weight_engine.get_hk_us_pair('09988.HK')
        self.assertIsNotNone(pair, "阿里巴巴应该有美股配对")
        self.assertEqual(pair['us_stock'], 'BABA')

        # 美团
        pair = self.engine._weight_engine.get_hk_us_pair('03690.HK')
        self.assertIsNotNone(pair, "美团应该有美股配对")
        self.assertEqual(pair['us_stock'], 'MPNGY')

    # ============================================================
    # 测试 3: 混合权重计算
    # ============================================================
    def test_hybrid_weight_calculation_basic(self):
        """测试基础权重计算"""
        weight, details = self.engine._calc_weight('confluence_v2', 'neutral', None)

        # 基础权重
        self.assertEqual(details['base_weight'], 1.5, "confluence_v2 基础权重应为 1.5")

        # 市场调整（中性市场）
        self.assertEqual(details['market_adjustment'], 1.0, "中性市场调整应为 1.0")

        # 最终权重
        self.assertEqual(weight, 1.5, "中性市场下权重应等于基础权重")

    def test_hybrid_weight_trending_market(self):
        """测试趋势市场权重计算"""
        # 趋势策略在趋势市场应获得增益
        weight, details = self.engine._calc_weight('supertrend', 'trending', None)

        self.assertGreater(
            details['market_adjustment'],
            1.0,
            "趋势策略在趋势市场应获得增益"
        )

        # 震荡策略在趋势市场应被衰减
        weight_oscillator, details_osc = self.engine._calc_weight('rsi', 'trending', None)
        self.assertLess(
            details_osc['market_adjustment'],
            1.0,
            "震荡策略在趋势市场应被衰减"
        )

    def test_hybrid_weight_ranging_market(self):
        """测试震荡市场权重计算"""
        # 震荡策略在震荡市场应获得增益
        weight, details = self.engine._calc_weight('rsi', 'ranging', None)

        self.assertGreater(
            details['market_adjustment'],
            1.0,
            "震荡策略在震荡市场应获得增益"
        )

        # 趋势策略在震荡市场应被衰减
        weight_trend, details_trend = self.engine._calc_weight('supertrend', 'ranging', None)
        self.assertLess(
            details_trend['market_adjustment'],
            1.0,
            "趋势策略在震荡市场应被衰减"
        )

    # ============================================================
    # 测试 4: 权重详情返回
    # ============================================================
    def test_weight_details_return(self):
        """测试权重详情返回"""
        weight, details = self.engine._calc_weight('confluence_v2', 'neutral', None)

        # 验证返回类型
        self.assertIsInstance(details, dict, "权重详情应为字典")

        # 验证必需字段
        required_fields = [
            'base_weight',
            'market_adjustment',
            'scenario_weight',
            'news_multiplier',
            'hybrid_adjustment',
        ]

        for field in required_fields:
            self.assertIn(field, details, f"权重详情应包含 {field}")

    # ============================================================
    # 测试 5: 情绪策略突发新闻权重倍增
    # ============================================================
    def test_sentiment_strategy_news_multiplier(self):
        """测试情绪策略突发新闻权重倍增"""
        # 注意：这个测试需要实际的新闻数据，这里只测试逻辑
        # 实际测试需要 mock _check_breaking_news 方法

        # 默认情况下（无突发新闻），news_multiplier 应为 1.0
        weight, details = self.engine._calc_weight('sentiment_ai', 'neutral', '00700.HK')
        self.assertEqual(
            details['news_multiplier'],
            1.0,
            "无突发新闻时，新闻倍增器应为 1.0"
        )

    # ============================================================
    # 测试 6: 混合架构开关
    # ============================================================
    def test_hybrid_architecture_switch(self):
        """测试混合架构开关"""
        # HYBRID_WEIGHT_ENABLED 控制混合架构启用
        self.assertIsInstance(
            HYBRID_WEIGHT_ENABLED,
            bool,
            "HYBRID_WEIGHT_ENABLED 应为布尔值"
        )

    # ============================================================
    # 测试 7: 权重计算结果范围
    # ============================================================
    def test_weight_calculation_range(self):
        """测试权重计算结果范围"""
        strategies = ['confluence_v2', 'supertrend', 'sentiment_ai', 'event_driven']
        regimes = ['trending', 'ranging', 'neutral']

        for strategy in strategies:
            for regime in regimes:
                weight, details = self.engine._calc_weight(strategy, regime, None)

                # 权重应为正数
                self.assertGreater(weight, 0, f"{strategy} {regime} 权重应为正数")

                # 权重不应过大（最大增益：1.5 * 1.5 * 2.0 * 1.2 = 5.4）
                self.assertLessEqual(
                    weight,
                    10.0,
                    f"{strategy} {regime} 权重不应过大"
                )


class TestWeightEngineDirect(unittest.TestCase):
    """WeightEngine 直接测试"""

    def setUp(self):
        """初始化 WeightEngine"""
        if not HYBRID_WEIGHT_ENABLED:
            self.skipTest("混合架构未启用")

        self.engine = ConsensusEngine()
        self.weight_engine = self.engine._weight_engine

    def test_news_weight_calculation(self):
        """测试新闻权重计算"""
        if not self.weight_engine:
            self.skipTest("WeightEngine 未初始化")

        # 无新闻情况
        weight, factor = self.weight_engine.calculate_news_weight([], [])
        self.assertEqual(weight, 0.0, "无新闻时权重应为 0")

        # 重大新闻情况（模拟）
        major_news = [
            {'importance': 'major', 'sentiment_score': 0.8, 'confidence': 0.9},
            {'importance': 'major', 'sentiment_score': 0.7, 'confidence': 0.85},
        ]
        sources = ['sina', 'tencent']

        weight, factor = self.weight_engine.calculate_news_weight(major_news, sources)
        self.assertEqual(weight, 0.40, "重大新闻权重应为 40%")

    def test_hk_us_weight_calculation(self):
        """测试 HK/US 权重计算"""
        if not self.weight_engine:
            self.skipTest("WeightEngine 未初始化")

        # 腾讯控股：美股上涨，港股上涨（同向）
        weight, factor = self.weight_engine.calculate_hk_us_weight(
            '00700.HK',
            hk_price_change=0.02,  # 港股上涨 2%
            us_price_change=0.03,  # 美股上涨 3%
        )

        # 高相关性 + 同向，权重应大于 0
        if factor and factor.enabled:
            self.assertGreater(weight, 0, "同向跟随权重应大于 0")

    def test_technical_weight_calculation(self):
        """测试技术权重计算"""
        if not self.weight_engine:
            self.skipTest("WeightEngine 未初始化")

        # 技术权重 = 100% - 新闻权重 - HK/US 权重
        news_weight = 0.25
        hk_us_weight = 0.20

        tech_weight, factor = self.weight_engine.calculate_technical_weight(
            news_weight, hk_us_weight, {}
        )

        # 技术权重应在 25-100% 范围内
        self.assertGreaterEqual(tech_weight, 0.25, "技术权重最小应为 25%")
        self.assertLessEqual(tech_weight, 1.0, "技术权重最大应为 100%")


def run_tests():
    """运行测试"""
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    # 添加测试
    suite.addTests(loader.loadTestsFromTestCase(TestHybridWeightArchitecture))
    suite.addTests(loader.loadTestsFromTestCase(TestWeightEngineDirect))

    # 运行测试
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    # 返回测试结果
    return result.wasSuccessful()


if __name__ == '__main__':
    import logging
    logging.basicConfig(level=logging.WARNING)  # 抑制日志，只显示测试结果
    success = run_tests()
    sys.exit(0 if success else 1)
