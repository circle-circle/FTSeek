#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
权重分配系统单元测试

测试覆盖:
1. 新闻权重计算
2. 港股美股相关性计算
3. 权重归一化
4. 约束验证
5. 决策日志

验收标准:
- 覆盖率≥80%
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, '/opt/ftseek/FTSeek')

from modules.news_monitor import (
    NewsMonitor, NewsItem, NewsAggregation,
    NewsSource, NewsSentiment, NewsImportance
)
from modules.decision_log import (
    DecisionLog, DecisionRecord, WeightAllocation,
    NewsFactor, HkUsFactor, TechnicalFactor,
    create_decision_record
)
from strategies.weight_engine import (
    WeightEngine, WeightResult, WeightConfig
)


class TestNewsMonitor(unittest.TestCase):
    """新闻监控器测试"""
    
    def setUp(self):
        """设置测试环境"""
        self.monitor = NewsMonitor()
    
    def tearDown(self):
        """清理测试环境"""
        asyncio.run(self.monitor.close())
    
    def test_classify_importance_major(self):
        """测试重大新闻分类"""
        # 需要 2+ 个重大关键词才算是重大新闻
        title = "腾讯财报超预期 净利润增长 50% 并购重组"
        content = "腾讯发布 2026 年 Q1 财报，净利润大幅增长，宣布重大合同"
        
        importance = self.monitor._classify_importance(title, content)
        
        self.assertEqual(importance, NewsImportance.MAJOR)
    
    def test_classify_importance_normal(self):
        """测试一般新闻分类"""
        # 需要 1 个重大关键词才算是普通新闻
        title = "腾讯推出新游戏产品 财报相关"
        content = "公司发布新产品"
        
        importance = self.monitor._classify_importance(title, content)
        
        self.assertEqual(importance, NewsImportance.NORMAL)
    
    def test_classify_importance_minor(self):
        """测试次要新闻分类"""
        title = "腾讯办公大楼日常"
        content = "普通新闻内容"
        
        importance = self.monitor._classify_importance(title, content)
        
        self.assertEqual(importance, NewsImportance.MINOR)
    
    def test_analyze_sentiment_keywords_positive(self):
        """测试正面情感分析"""
        title = "腾讯业绩大增 盈利创新高"
        content = "公司表现优异"
        
        sentiment, score = self.monitor._analyze_sentiment_keywords(title, content)
        
        self.assertEqual(sentiment, NewsSentiment.POSITIVE)
        self.assertGreater(score, 0.2)
    
    def test_analyze_sentiment_keywords_negative(self):
        """测试负面情感分析"""
        title = "腾讯业绩下滑 亏损扩大"
        content = "公司表现不佳"
        
        sentiment, score = self.monitor._analyze_sentiment_keywords(title, content)
        
        self.assertEqual(sentiment, NewsSentiment.NEGATIVE)
        self.assertLess(score, -0.2)
    
    def test_analyze_sentiment_keywords_neutral(self):
        """测试中性情感分析"""
        title = "腾讯发布日常公告"
        content = "无特别内容"
        
        sentiment, score = self.monitor._analyze_sentiment_keywords(title, content)
        
        self.assertEqual(sentiment, NewsSentiment.NEUTRAL)
        self.assertGreaterEqual(score, -0.2)
        self.assertLessEqual(score, 0.2)
    
    def test_calculate_weight_major_news(self):
        """测试重大新闻权重计算 (40%)"""
        news_items = [
            NewsItem(
                id="1", title="财报超预期", content="",
                source=NewsSource.SINA, publish_time=datetime.now(),
                stocks=["00700.HK"], importance=NewsImportance.MAJOR
            ),
            NewsItem(
                id="2", title="并购消息", content="",
                source=NewsSource.TENCENT, publish_time=datetime.now(),
                stocks=["00700.HK"], importance=NewsImportance.MAJOR
            )
        ]
        
        weight = self.monitor.calculate_weight(news_items)
        
        self.assertEqual(weight, 0.40)
    
    def test_calculate_weight_normal_news(self):
        """测试一般新闻权重计算 (25%)"""
        news_items = [
            NewsItem(
                id="1", title="新产品发布", content="",
                source=NewsSource.SINA, publish_time=datetime.now(),
                stocks=["00700.HK"], importance=NewsImportance.NORMAL
            ),
            NewsItem(
                id="2", title="合作消息", content="",
                source=NewsSource.TENCENT, publish_time=datetime.now(),
                stocks=["00700.HK"], importance=NewsImportance.NORMAL
            ),
            NewsItem(
                id="3", title="日常公告", content="",
                source=NewsSource.FUTU, publish_time=datetime.now(),
                stocks=["00700.HK"], importance=NewsImportance.NORMAL
            )
        ]
        
        weight = self.monitor.calculate_weight(news_items)
        
        self.assertEqual(weight, 0.25)
    
    def test_calculate_weight_no_news(self):
        """测试无新闻权重计算 (0%)"""
        news_items = []
        
        weight = self.monitor.calculate_weight(news_items)
        
        self.assertEqual(weight, 0.0)
    
    def test_generate_news_id_unique(self):
        """测试新闻 ID 唯一性"""
        id1 = self.monitor._generate_news_id("标题", "sina", datetime.now())
        id2 = self.monitor._generate_news_id("标题", "sina", datetime.now() + timedelta(seconds=1))
        
        self.assertNotEqual(id1, id2)


class TestWeightAllocation(unittest.TestCase):
    """权重分配测试"""
    
    def test_weight_allocation_validation_valid(self):
        """测试有效权重验证"""
        allocation = WeightAllocation(
            news_weight=0.25,
            hk_us_weight=0.25,
            technical_weight=0.50,
            total_weight=1.0
        )
        
        valid, msg = allocation.validate()
        
        self.assertTrue(valid)
        self.assertEqual(msg, "通过")
    
    def test_weight_allocation_validation_invalid(self):
        """测试无效权重验证"""
        allocation = WeightAllocation(
            news_weight=0.30,
            hk_us_weight=0.30,
            technical_weight=0.50,
            total_weight=1.10  # 错误
        )
        
        valid, msg = allocation.validate()
        
        self.assertFalse(valid)
        self.assertIn("≠100%", msg)
    
    def test_weight_allocation_to_dict(self):
        """测试权重分配转字典"""
        allocation = WeightAllocation(
            news_weight=0.25,
            hk_us_weight=0.25,
            technical_weight=0.50,
            total_weight=1.0
        )
        
        data = allocation.to_dict()
        
        self.assertEqual(data['news_weight'], 0.25)
        self.assertEqual(data['hk_us_weight'], 0.25)
        self.assertEqual(data['technical_weight'], 0.50)
        self.assertEqual(data['total_weight'], 1.0)


class TestWeightEngine(unittest.TestCase):
    """权重引擎测试"""
    
    def setUp(self):
        """设置测试环境"""
        self.engine = WeightEngine()
    
    def tearDown(self):
        """清理测试环境"""
        asyncio.run(self.engine.close())
    
    def test_get_hk_us_pair_found(self):
        """测试获取港股美股配对 (存在)"""
        pair = self.engine.get_hk_us_pair("00700.HK")
        
        self.assertIsNotNone(pair)
        self.assertEqual(pair['hk_stock'], "00700.HK")
        self.assertEqual(pair['us_stock'], "TCEHY")
        self.assertTrue(pair.get('enabled', False))
    
    def test_get_hk_us_pair_not_found(self):
        """测试获取港股美股配对 (不存在)"""
        pair = self.engine.get_hk_us_pair("INVALID.HK")
        
        self.assertIsNone(pair)
    
    def test_get_hk_us_pair_disabled(self):
        """测试获取港股美股配对 (未启用)"""
        pair = self.engine.get_hk_us_pair("00388.HK")
        
        # 相关性<0.6，应该未启用
        if pair:
            self.assertFalse(pair.get('enabled', True))
    
    def test_calculate_correlation_from_mapping(self):
        """测试从映射表获取相关性"""
        correlation = self.engine.calculate_correlation("00700.HK", "TCEHY")
        
        # 应该从映射表获取预计算的相关性
        self.assertGreaterEqual(correlation, 0.0)
        self.assertLessEqual(correlation, 1.0)
    
    def test_calculate_news_weight_empty(self):
        """测试空新闻列表权重"""
        weight, factor = self.engine.calculate_news_weight([], [])
        
        self.assertEqual(weight, 0.0)
        self.assertEqual(factor.news_count, 0)
    
    def test_calculate_hk_us_weight_no_pair(self):
        """测试无配对时港股美股权重"""
        weight, factor = self.engine.calculate_hk_us_weight(
            "INVALID.HK", 0.02, 0.015
        )
        
        self.assertEqual(weight, 0.0)
        self.assertIsNone(factor)
    
    def test_calculate_hk_us_weight_low_correlation(self):
        """测试低相关性时港股美股权重"""
        # 00388.HK 相关性<0.6
        weight, factor = self.engine.calculate_hk_us_weight(
            "00388.HK", 0.02, 0.015
        )
        
        self.assertEqual(weight, 0.0)
        if factor:
            self.assertFalse(factor.enabled)
    
    def test_calculate_technical_weight_default(self):
        """测试默认技术权重"""
        technical_weight, factor = self.engine.calculate_technical_weight(
            news_weight=0.25,
            hk_us_weight=0.25
        )
        
        # 技术权重 = 100% - 25% - 25% = 50%
        self.assertAlmostEqual(technical_weight, 0.50, places=2)
        self.assertGreaterEqual(technical_weight, 0.25)
        self.assertLessEqual(technical_weight, 1.0)
    
    def test_calculate_technical_weight_min_bound(self):
        """测试技术权重下限"""
        # 新闻 40% + 港股美股 35% = 75%
        # 技术权重应该是 25%
        technical_weight, factor = self.engine.calculate_technical_weight(
            news_weight=0.40,
            hk_us_weight=0.35
        )
        
        self.assertAlmostEqual(technical_weight, 0.25, places=2)
    
    def test_validate_constraints_weight_sum(self):
        """测试权重总和约束验证"""
        result = WeightResult(
            stock="00700.HK",
            timestamp=datetime.now(),
            news_weight=0.30,
            hk_us_weight=0.30,
            technical_weight=0.50,
            total_weight=1.10,  # 错误
            news_factor=None,
            hk_us_factor=None,
            technical_factor=TechnicalFactor({}, 0.5, 0.8),
            decision_id="D123",
            constraints_valid=True,
            constraint_errors=[]
        )
        
        valid, errors = self.engine.validate_constraints(result, 3)
        
        self.assertFalse(valid)
        self.assertTrue(any("权重总和" in e for e in errors))
    
    def test_validate_constraints_news_sources(self):
        """测试新闻源数量约束验证"""
        news_factor = NewsFactor(
            news_count=1,
            major_news_count=0,
            sources=['sina'],  # 只有 1 个源
            sentiment='neutral',
            sentiment_score=0.0,
            confidence=0.6,
            importance='minor',
            latency_ms=100
        )
        
        result = WeightResult(
            stock="00700.HK",
            timestamp=datetime.now(),
            news_weight=0.0,
            hk_us_weight=0.0,
            technical_weight=1.0,
            total_weight=1.0,
            news_factor=news_factor,
            hk_us_factor=None,
            technical_factor=TechnicalFactor({}, 0.5, 0.8),
            decision_id="D123",
            constraints_valid=True,
            constraint_errors=[]
        )
        
        valid, errors = self.engine.validate_constraints(result, 3)
        
        self.assertFalse(valid)
        self.assertTrue(any("新闻源" in e for e in errors))
    
    def test_validate_constraints_daily_trades(self):
        """测试日交易数量约束验证"""
        result = WeightResult(
            stock="00700.HK",
            timestamp=datetime.now(),
            news_weight=0.25,
            hk_us_weight=0.25,
            technical_weight=0.50,
            total_weight=1.0,
            news_factor=None,
            hk_us_factor=None,
            technical_factor=TechnicalFactor({}, 0.5, 0.8),
            decision_id="D123",
            constraints_valid=True,
            constraint_errors=[]
        )
        
        valid, errors = self.engine.validate_constraints(result, 6)  # 超过 5 笔
        
        self.assertFalse(valid)
        self.assertTrue(any("日交易" in e for e in errors))
    
    def test_validate_constraints_decision_id(self):
        """测试决策 ID 约束验证"""
        result = WeightResult(
            stock="00700.HK",
            timestamp=datetime.now(),
            news_weight=0.25,
            hk_us_weight=0.25,
            technical_weight=0.50,
            total_weight=1.0,
            news_factor=None,
            hk_us_factor=None,
            technical_factor=TechnicalFactor({}, 0.5, 0.8),
            decision_id="",  # 空 ID
            constraints_valid=True,
            constraint_errors=[]
        )
        
        valid, errors = self.engine.validate_constraints(result, 3)
        
        self.assertFalse(valid)
        self.assertTrue(any("决策链路" in e for e in errors))
    
    def test_validate_constraints_all_valid(self):
        """测试所有约束通过"""
        result = WeightResult(
            stock="00700.HK",
            timestamp=datetime.now(),
            news_weight=0.25,
            hk_us_weight=0.25,
            technical_weight=0.50,
            total_weight=1.0,
            news_factor=None,
            hk_us_factor=None,
            technical_factor=TechnicalFactor({}, 0.5, 0.8),
            decision_id="D123",
            constraints_valid=True,
            constraint_errors=[]
        )
        
        valid, errors = self.engine.validate_constraints(result, 3)
        
        self.assertTrue(valid)
        self.assertEqual(len(errors), 0)


class TestDecisionLog(unittest.TestCase):
    """决策日志测试"""
    
    def setUp(self):
        """设置测试环境"""
        # 使用临时数据库
        self.temp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.temp_db.close()
        self.log = DecisionLog(db_path=self.temp_db.name)
    
    def tearDown(self):
        """清理测试环境"""
        self.log = None
        if os.path.exists(self.temp_db.name):
            os.remove(self.temp_db.name)
    
    def test_log_decision(self):
        """测试记录决策"""
        record = create_decision_record(
            stock="00700.HK",
            action="BUY",
            news_weight=0.25,
            hk_us_weight=0.25,
            technical_weight=0.50
        )
        
        decision_id = self.log.log_decision(record)
        
        self.assertIsNotNone(decision_id)
        self.assertTrue(decision_id.startswith("D"))
    
    def test_get_decision(self):
        """测试获取决策"""
        record = create_decision_record(
            stock="00700.HK",
            action="BUY",
            news_weight=0.25,
            hk_us_weight=0.25,
            technical_weight=0.50
        )
        
        decision_id = self.log.log_decision(record)
        retrieved = self.log.get_decision(decision_id)
        
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.decision_id, decision_id)
        self.assertEqual(retrieved.stock, "00700.HK")
    
    def test_get_decision_not_found(self):
        """测试获取不存在的决策"""
        retrieved = self.log.get_decision("INVALID_ID")
        
        self.assertIsNone(retrieved)
    
    def test_query_decisions_by_stock(self):
        """测试按股票查询决策"""
        # 创建多条记录
        for stock in ["00700.HK", "09988.HK", "00700.HK"]:
            record = create_decision_record(
                stock=stock,
                action="BUY",
                news_weight=0.25,
                hk_us_weight=0.25,
                technical_weight=0.50
            )
            self.log.log_decision(record)
        
        # 查询特定股票
        decisions = self.log.query_decisions(stock="00700.HK")
        
        self.assertEqual(len(decisions), 2)
        self.assertTrue(all(d.stock == "00700.HK" for d in decisions))
    
    def test_update_status(self):
        """测试更新状态"""
        record = create_decision_record(
            stock="00700.HK",
            action="BUY",
            news_weight=0.25,
            hk_us_weight=0.25,
            technical_weight=0.50
        )
        # 初始状态就是 pending
        
        decision_id = self.log.log_decision(record)
        
        # 更新状态
        exec_time = datetime.now()
        self.log.update_status(
            decision_id, 'executed',
            execution_time=exec_time,
            execution_price=100.0
        )
        
        # 验证更新
        retrieved = self.log.get_decision(decision_id)
        self.assertEqual(retrieved.status, 'executed')
        self.assertEqual(retrieved.execution_price, 100.0)
    
    def test_get_daily_decisions(self):
        """测试获取今日决策"""
        # 创建今日记录
        for i in range(3):
            record = create_decision_record(
                stock=f"00700{i}.HK",
                action="BUY",
                news_weight=0.25,
                hk_us_weight=0.25,
                technical_weight=0.50
            )
            self.log.log_decision(record)
        
        decisions = self.log.get_daily_decisions()
        
        self.assertGreaterEqual(len(decisions), 3)
    
    def test_validate_daily_limit_exceeded(self):
        """测试日交易限制超限"""
        # 创建 6 条记录 (超过 5 笔限制)
        for i in range(6):
            record = create_decision_record(
                stock=f"00700{i}.HK",
                action="BUY",
                news_weight=0.25,
                hk_us_weight=0.25,
                technical_weight=0.50
            )
            self.log.log_decision(record)
        
        exceeded, count = self.log.validate_daily_limit(limit=5)
        
        self.assertTrue(exceeded)
        self.assertEqual(count, 6)
    
    def test_validate_daily_limit_ok(self):
        """测试日交易限制未超限"""
        # 创建 3 条记录
        for i in range(3):
            record = create_decision_record(
                stock=f"00700{i}.HK",
                action="BUY",
                news_weight=0.25,
                hk_us_weight=0.25,
                technical_weight=0.50
            )
            self.log.log_decision(record)
        
        exceeded, count = self.log.validate_daily_limit(limit=5)
        
        self.assertFalse(exceeded)
        self.assertEqual(count, 3)
    
    def test_get_trace(self):
        """测试获取追溯链路"""
        record = create_decision_record(
            stock="00700.HK",
            action="BUY",
            news_weight=0.25,
            hk_us_weight=0.25,
            technical_weight=0.50
        )
        
        decision_id = self.log.log_decision(record)
        trace = self.log.get_trace(decision_id)
        
        self.assertIn('decision_id', trace)
        self.assertIn('trace', trace)
        self.assertIn('validation', trace)
        self.assertTrue(trace['validation']['weight_sum_valid'])
    
    def test_export_decisions(self):
        """测试导出决策"""
        # 创建记录
        for i in range(3):
            record = create_decision_record(
                stock=f"00700{i}.HK",
                action="BUY",
                news_weight=0.25,
                hk_us_weight=0.25,
                technical_weight=0.50
            )
            self.log.log_decision(record)
        
        # 导出
        temp_output = tempfile.NamedTemporaryFile(suffix='.json', delete=False)
        temp_output.close()
        
        try:
            output_path = self.log.export_decisions(temp_output.name)
            
            self.assertTrue(os.path.exists(output_path))
            
            with open(output_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            self.assertIn('export_time', data)
            self.assertIn('count', data)
            self.assertEqual(data['count'], 3)
            self.assertIn('decisions', data)
            
        finally:
            if os.path.exists(temp_output.name):
                os.remove(temp_output.name)
    
    def test_get_stats(self):
        """测试获取统计"""
        # 创建记录
        for i in range(5):
            record = create_decision_record(
                stock=f"00700{i}.HK",
                action="BUY",
                news_weight=0.25,
                hk_us_weight=0.25,
                technical_weight=0.50
            )
            self.log.log_decision(record)
        
        stats = self.log.get_stats()
        
        self.assertIn('count', stats)
        self.assertGreaterEqual(stats['count'], 5)
        self.assertIn('status_breakdown', stats)
        self.assertIn('avg_weights', stats)


class TestIntegration(unittest.TestCase):
    """集成测试"""
    
    def setUp(self):
        """设置测试环境"""
        self.engine = WeightEngine()
        self.temp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.temp_db.close()
        self.engine.decision_log = DecisionLog(db_path=self.temp_db.name)
    
    def tearDown(self):
        """清理测试环境"""
        asyncio.run(self.engine.close())
        if os.path.exists(self.temp_db.name):
            os.remove(self.temp_db.name)
    
    def test_full_weight_calculation(self):
        """测试完整权重计算流程"""
        async def run_test():
            result = await self.engine.calculate_weights(
                stock="00700.HK",
                action="BUY",
                hk_price_change=0.02,
                us_price_change=0.015,
                technical_indicators={'rsi': 55, 'macd': 0.02, 'bb': 0.6}
            )
            
            # 验证权重总和=100%
            self.assertAlmostEqual(result.total_weight, 1.0, places=2)
            
            # 验证权重范围
            self.assertGreaterEqual(result.news_weight, 0.0)
            self.assertLessEqual(result.news_weight, 0.40)
            
            self.assertGreaterEqual(result.hk_us_weight, 0.0)
            self.assertLessEqual(result.hk_us_weight, 0.35)
            
            self.assertGreaterEqual(result.technical_weight, 0.25)
            self.assertLessEqual(result.technical_weight, 1.0)
            
            # 验证决策 ID 存在
            self.assertIsNotNone(result.decision_id)
            self.assertTrue(len(result.decision_id) > 0)
            
            # 验证追溯链路
            trace = self.engine.get_trace(result.decision_id)
            self.assertIn('decision_id', trace)
            self.assertEqual(trace['decision_id'], result.decision_id)
            
            return result
        
        result = asyncio.run(run_test())
        print(f"\n集成测试结果:")
        print(f"  新闻权重：{result.news_weight:.2%}")
        print(f"  港股美股权重：{result.hk_us_weight:.2%}")
        print(f"  技术权重：{result.technical_weight:.2%}")
        print(f"  总权重：{result.total_weight:.2%}")
        print(f"  约束验证：{'通过' if result.constraints_valid else '失败'}")


if __name__ == '__main__':
    # 运行测试
    unittest.main(verbosity=2)
