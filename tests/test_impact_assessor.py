#!/usr/bin/env python3
"""
影响评估器单元测试

测试影响评估模型的核心功能:
- 评估维度完整性
- 情绪评分准确性
- 影响程度合理性
- 建议操作逻辑
"""
import unittest
from unittest.mock import Mock, patch, MagicMock
import sys
import os

# 添加项目路径
sys.path.insert(0, '/opt/ftseek/FTSeek')

from app.analyzers.impact_assessor import (
    ImpactResult, 
    ImpactAssessor,
    assess_news_impact
)
from app.analyzers.news_analyzer import NewsItem


class TestImpactResult(unittest.TestCase):
    """测试 ImpactResult 数据类"""
    
    def test_impact_result_creation(self):
        """测试创建影响评估结果"""
        result = ImpactResult(
            direction='利好',
            sentiment=0.8,
            impact_level=8,
            relevance=0.9,
            action='买入',
            reason='业绩超预期增长'
        )
        
        self.assertEqual(result.direction, '利好')
        self.assertEqual(result.sentiment, 0.8)
        self.assertEqual(result.impact_level, 8)
        self.assertEqual(result.relevance, 0.9)
        self.assertEqual(result.action, '买入')
        self.assertEqual(result.reason, '业绩超预期增长')
    
    def test_impact_result_to_dict(self):
        """测试转换为字典"""
        result = ImpactResult(
            direction='利空',
            sentiment=-0.6,
            impact_level=7,
            relevance=0.8,
            action='卖出',
            reason='监管政策收紧'
        )
        
        d = result.to_dict()
        
        self.assertEqual(d['direction'], '利空')
        self.assertEqual(d['sentiment'], -0.6)
        self.assertEqual(d['impact_level'], 7)
        self.assertEqual(d['relevance'], 0.8)
        self.assertEqual(d['action'], '卖出')
        self.assertEqual(d['reason'], '监管政策收紧')
    
    def test_impact_result_default_values(self):
        """测试默认值"""
        result = ImpactResult()
        
        self.assertEqual(result.direction, '中性')
        self.assertEqual(result.sentiment, 0.0)
        self.assertEqual(result.impact_level, 5)
        self.assertEqual(result.relevance, 0.5)
        self.assertEqual(result.action, '观望')
        self.assertEqual(result.reason, '')


class TestImpactAssessor(unittest.TestCase):
    """测试 ImpactAssessor 类"""
    
    def setUp(self):
        """准备测试数据"""
        self.assessor = ImpactAssessor()
        
        self.sample_news = NewsItem(
            id='test123',
            code='HK.00700',
            title='腾讯控股发布超预期财报，净利润增长 50%',
            content='腾讯控股今日发布 2026 年 Q1 财报，显示净利润同比增长 50%，',
            source='sina',
            url='https://example.com/news/123',
            published_at='1740825600'
        )
    
    def test_assess_news_impact_with_llm(self):
        """测试使用大模型评估新闻影响 (使用降级模式模拟)"""
        # 使用降级模式测试基本功能
        assessor = ImpactAssessor(enable_llm=False)
        
        result = assessor.assess_news_impact(self.sample_news, 'HK.00700')
        
        self.assertIsInstance(result, ImpactResult)
        self.assertIn(result.direction, ['利好', '利空', '中性'])
        self.assertIn(result.action, ['买入', '卖出', '观望'])
        self.assertGreaterEqual(result.sentiment, -1.0)
        self.assertLessEqual(result.sentiment, 1.0)
        self.assertGreaterEqual(result.impact_level, 1)
        self.assertLessEqual(result.impact_level, 10)
        self.assertTrue(result.is_fallback)  # 降级模式
    
    def test_assess_news_impact_fallback(self):
        """测试降级方案 (关键词分析)"""
        # 创建没有 LLM 的 assessor
        assessor = ImpactAssessor(enable_llm=False)
        
        # 正面新闻
        positive_news = NewsItem(
            title='公司盈利大幅增长，超预期',
            content='业绩强劲，分析师看好',
            code='HK.00700'
        )
        
        result = assessor.assess_news_impact(positive_news, 'HK.00700')
        
        self.assertEqual(result.direction, '利好')
        self.assertGreater(result.sentiment, 0)
        self.assertGreater(result.impact_level, 5)
        self.assertEqual(result.action, '买入')
        self.assertTrue(result.is_fallback)
    
    def test_assess_news_impact_negative(self):
        """测试负面新闻评估"""
        assessor = ImpactAssessor(enable_llm=False)
        
        negative_news = NewsItem(
            title='公司亏损严重，面临退市风险',
            content='业绩恶化，债务违约，被监管调查',
            code='HK.00700'
        )
        
        result = assessor.assess_news_impact(negative_news, 'HK.00700')
        
        self.assertEqual(result.direction, '利空')
        self.assertLess(result.sentiment, 0)
        self.assertGreater(result.impact_level, 5)
        self.assertEqual(result.action, '卖出')
    
    def test_assess_news_impact_neutral(self):
        """测试中性新闻评估"""
        assessor = ImpactAssessor(enable_llm=False)
        
        neutral_news = NewsItem(
            title='公司召开年度股东大会',
            content='会议讨论了日常经营事项',
            code='HK.00700'
        )
        
        result = assessor.assess_news_impact(neutral_news, 'HK.00700')
        
        self.assertEqual(result.direction, '中性')
        self.assertAlmostEqual(result.sentiment, 0.0, delta=0.3)
    
    def test_sentiment_bounds(self):
        """测试情绪评分边界"""
        assessor = ImpactAssessor(enable_llm=False)
        
        # 极度正面
        very_positive = NewsItem(
            title='重大利好！盈利增长 500%，创新高',
            content='业绩超预期，分析师纷纷上调目标价',
            code='HK.00700'
        )
        
        result = assessor.assess_news_impact(very_positive, 'HK.00700')
        self.assertLessEqual(result.sentiment, 1.0)
        self.assertGreaterEqual(result.sentiment, 0.3)
        
        # 极度负面
        very_negative = NewsItem(
            title='重大利空！破产清算，退市',
            content='债务违约，被立案调查，业绩巨亏',
            code='HK.00700'
        )
        
        result = assessor.assess_news_impact(very_negative, 'HK.00700')
        self.assertGreaterEqual(result.sentiment, -1.0)
        self.assertLessEqual(result.sentiment, -0.3)
    
    def test_impact_level_bounds(self):
        """测试影响程度边界"""
        assessor = ImpactAssessor(enable_llm=False)
        
        news = NewsItem(
            title='测试新闻',
            content='测试内容',
            code='HK.00700'
        )
        
        result = assessor.assess_news_impact(news, 'HK.00700')
        
        self.assertGreaterEqual(result.impact_level, 1)
        self.assertLessEqual(result.impact_level, 10)
    
    def test_relevance_score(self):
        """测试关联度评分"""
        assessor = ImpactAssessor(enable_llm=False)
        
        # 高关联度新闻 (包含股票代码)
        high_relevance = NewsItem(
            title='腾讯控股 (00700.HK) 发布财报',
            content='腾讯控股业绩表现',
            code='HK.00700'
        )
        
        result = assessor.assess_news_impact(high_relevance, 'HK.00700')
        self.assertGreater(result.relevance, 0.5)
        
        # 低关联度新闻 (无代码的通用财经新闻)
        low_relevance = NewsItem(
            title='央行宣布降准',
            content='宏观经济政策调整',
            code=''  # 无股票代码
        )
        
        result = assessor.assess_news_impact(low_relevance, 'HK.00700')
        # 通用新闻基础关联度 0.5
        self.assertAlmostEqual(result.relevance, 0.5, delta=0.1)
    
    def test_action_mapping(self):
        """测试建议操作映射"""
        assessor = ImpactAssessor(enable_llm=False)
        
        # 强烈利好 -> 买入
        bullish_news = NewsItem(
            title='盈利暴增 500%，重大利好',
            content='业绩超预期，增长强劲',
            code='HK.00700'
        )
        result = assessor.assess_news_impact(bullish_news, 'HK.00700')
        self.assertEqual(result.action, '买入')
        
        # 强烈利空 -> 卖出
        bearish_news = NewsItem(
            title='巨额亏损，面临退市',
            content='业绩恶化，债务违约',
            code='HK.00700'
        )
        result = assessor.assess_news_impact(bearish_news, 'HK.00700')
        self.assertEqual(result.action, '卖出')
        
        # 中性 -> 观望
        neutral_news = NewsItem(
            title='公司正常经营',
            content='无重大变化',
            code='HK.00700'
        )
        result = assessor.assess_news_impact(neutral_news, 'HK.00700')
        self.assertEqual(result.action, '观望')


class TestAssessNewsImpactFunction(unittest.TestCase):
    """测试便捷函数 assess_news_impact"""
    
    def test_convenience_function(self):
        """测试便捷函数调用"""
        news = NewsItem(
            title='测试新闻',
            content='测试内容',
            code='HK.00700'
        )
        
        result = assess_news_impact(news, 'HK.00700')
        
        self.assertIsInstance(result, ImpactResult)
        self.assertIn(result.direction, ['利好', '利空', '中性'])
        self.assertIn(result.action, ['买入', '卖出', '观望'])
        self.assertGreaterEqual(result.sentiment, -1.0)
        self.assertLessEqual(result.sentiment, 1.0)
        self.assertGreaterEqual(result.impact_level, 1)
        self.assertLessEqual(result.impact_level, 10)
        self.assertGreaterEqual(result.relevance, 0.0)
        self.assertLessEqual(result.relevance, 1.0)


class TestKeywordAnalysis(unittest.TestCase):
    """测试关键词分析 (降级方案)"""
    
    def setUp(self):
        self.assessor = ImpactAssessor(enable_llm=False)
    
    def test_positive_keywords(self):
        """测试正面关键词识别"""
        positive_texts = [
            '盈利增长',
            '业绩超预期',
            '利好政策',
            '股价创新高',
            '分析师看好'
        ]
        
        for text in positive_texts:
            news = NewsItem(title=text, content=text, code='HK.00700')
            result = self.assessor.assess_news_impact(news, 'HK.00700')
            self.assertGreater(result.sentiment, 0, f"Failed for: {text}")
    
    def test_negative_keywords(self):
        """测试负面关键词识别"""
        negative_texts = [
            '亏损',
            '业绩下滑',
            '利空消息',
            '股价暴跌',
            '监管调查'
        ]
        
        for text in negative_texts:
            news = NewsItem(title=text, content=text, code='HK.00700')
            result = self.assessor.assess_news_impact(news, 'HK.00700')
            self.assertLess(result.sentiment, 0, f"Failed for: {text}")
    
    def test_mixed_sentiment(self):
        """测试混合情绪"""
        news = NewsItem(
            title='盈利增长但增速放缓',
            content='业绩向好但面临挑战',
            code='HK.00700'
        )
        
        result = self.assessor.assess_news_impact(news, 'HK.00700')
        # 混合情绪应该接近中性
        self.assertGreaterEqual(result.sentiment, -0.3)
        self.assertLessEqual(result.sentiment, 0.3)


if __name__ == '__main__':
    unittest.main(verbosity=2)
