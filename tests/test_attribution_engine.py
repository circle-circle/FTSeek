#!/usr/bin/env python3
"""
交易归因系统单元测试

测试内容:
1. 归因引擎 - 规则归因/统计归因/LLM一致性检查
2. 因果链条摘要生成
3. 防御性更新机制
4. 交易时段检测
5. 每周优化脚本

运行方式:
    python -m pytest tests/test_attribution_engine.py -v
"""

import pytest
import sys
import os
from datetime import datetime
from unittest.mock import MagicMock, patch

# 添加项目根目录到路径
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.evidence.trade_evidence_chain import (
    TradeEvidenceChain, 
    EvidenceLayer, 
    AttributionLayer, 
    FeedbackLayer,
    detect_trading_session,
)
from app.evidence.attribution_engine import AttributionEngine


class TestTradingSessionDetection:
    """测试交易时段检测"""
    
    def test_pre_market_session(self):
        """测试盘前时段 09:00-09:30"""
        timestamp = "2026-04-12T09:15:00"
        session = detect_trading_session(timestamp)
        assert session == "pre_market"
    
    def test_opening_session(self):
        """测试开盘时段 09:30-10:00"""
        timestamp = "2026-04-12T09:45:00"
        session = detect_trading_session(timestamp)
        assert session == "opening"
    
    def test_morning_session(self):
        """测试上午时段 10:00-12:00"""
        timestamp = "2026-04-12T11:30:00"
        session = detect_trading_session(timestamp)
        assert session == "morning"
    
    def test_afternoon_session(self):
        """测试下午时段 13:00-15:00"""
        timestamp = "2026-04-12T14:00:00"
        session = detect_trading_session(timestamp)
        assert session == "afternoon"
    
    def test_closing_session(self):
        """测试尾盘时段 15:00-16:00"""
        timestamp = "2026-04-12T15:30:00"
        session = detect_trading_session(timestamp)
        assert session == "closing"
    
    def test_lunch_break(self):
        """测试午间休息 12:00-13:00"""
        timestamp = "2026-04-12T12:30:00"
        session = detect_trading_session(timestamp)
        # 应该归类到上午或返回空
        assert session in ["morning", ""]
    
    def test_after_hours(self):
        """测试盘后时段"""
        timestamp = "2026-04-12T17:00:00"
        session = detect_trading_session(timestamp)
        assert session == ""
    
    def test_early_morning(self):
        """测试凌晨时段"""
        timestamp = "2026-04-12T03:00:00"
        session = detect_trading_session(timestamp)
        assert session == ""


class TestRuleBasedAttribution:
    """测试规则归因"""
    
    def test_stop_loss_attribution(self):
        """测试止损触发归因"""
        chain = TradeEvidenceChain()
        chain.exit_reason = "stop_loss"
        chain.pnl_percent = -5.0
        
        engine = AttributionEngine()
        engine._rule_based_attribution(chain)
        
        assert "stop_loss_hit" in chain.attribution.risk_tags
    
    def test_high_slippage_attribution(self):
        """测试高滑点归因"""
        chain = TradeEvidenceChain()
        chain.attribution.slippage_bps = 8.0  # >5bps
        chain.pnl_percent = -2.0
        
        engine = AttributionEngine()
        engine._rule_based_attribution(chain)
        
        assert "slippage_high" in chain.attribution.execution_tags
        assert chain.attribution.execution_quality == "poor"
    
    def test_arbitrage_expired_attribution(self):
        """测试套利窗口过期归因"""
        chain = TradeEvidenceChain()
        chain.exit_reason = "arbitrage_expired"
        
        engine = AttributionEngine()
        engine._rule_based_attribution(chain)
        
        assert "arbitrage_expired" in chain.attribution.risk_tags
    
    def test_normal_exit_attribution(self):
        """测试正常离场无特殊标签"""
        chain = TradeEvidenceChain()
        chain.exit_reason = "target_reached"
        chain.attribution.slippage_bps = 2.0  # 正常滑点
        
        engine = AttributionEngine()
        engine._rule_based_attribution(chain)
        
        # 不应该有任何负面标签
        assert "stop_loss_hit" not in chain.attribution.risk_tags
        assert "slippage_high" not in chain.attribution.execution_tags


class TestLLMConsistencyCheck:
    """测试LLM一致性检查"""
    
    def test_llm_overconfident(self):
        """测试LLM过度自信检测"""
        chain = TradeEvidenceChain()
        chain.evidence.llm_confidence = 0.85
        chain.evidence.llm_reasoning = "强烈看涨，预计上涨3%"
        chain.pnl_percent = -3.2
        
        engine = AttributionEngine()
        result = engine.check_llm_consistency(chain)
        
        assert result['consistent'] == False
        assert any(issue['type'] == 'llm_overconfident' for issue in result['issues'])
    
    def test_llm_correct_high_confidence(self):
        """测试LLM高置信度且正确"""
        chain = TradeEvidenceChain()
        chain.evidence.llm_confidence = 0.85
        chain.evidence.llm_reasoning = "看涨"
        chain.pnl_percent = 3.5
        chain.attribution.direction_correct = True  # 设置方向正确
        
        engine = AttributionEngine()
        result = engine.check_llm_consistency(chain)
        
        # 高置信度且盈利，应该一致
        assert result['consistent'] == True
        assert len(result['issues']) == 0
    
    def test_llm_news_misread(self):
        """测试LLM新闻误读检测"""
        chain = TradeEvidenceChain()
        chain.evidence.llm_confidence = 0.75
        chain.evidence.llm_reasoning = "利好消息驱动，预计上涨"
        chain.evidence.news_sentiment = -0.3  # 负面新闻情绪
        chain.pnl_percent = -1.5
        
        engine = AttributionEngine()
        result = engine.check_llm_consistency(chain)
        
        assert result['consistent'] == False
        assert any(issue['type'] == 'llm_news_misread' for issue in result['issues'])
    
    def test_llm_false_confidence(self):
        """测试LLM虚假信心检测"""
        chain = TradeEvidenceChain()
        chain.evidence.llm_confidence = 0.90  # 非常高置信度
        chain.evidence.llm_reasoning = "确定性很高"
        chain.pnl_percent = -4.0  # 但严重亏损
        
        engine = AttributionEngine()
        result = engine.check_llm_consistency(chain)
        
        assert result['consistent'] == False
        # 应该检测到过度自信或虚假信心
        assert len(result['issues']) > 0
    
    def test_no_issues_when_consistent(self):
        """测试一致性良好时无问题"""
        chain = TradeEvidenceChain()
        chain.evidence.llm_confidence = 0.65
        chain.evidence.llm_reasoning = "温和看涨"
        chain.pnl_percent = 1.5
        
        engine = AttributionEngine()
        result = engine.check_llm_consistency(chain)
        
        assert result['consistent'] == True
        assert len(result['issues']) == 0


class TestDefensiveUpdate:
    """测试防御性更新机制"""
    
    def test_defensive_update_on_wrong_direction(self):
        """测试方向错误时触发防御性更新"""
        chain = TradeEvidenceChain()
        chain.evidence.llm_confidence = 0.80
        chain.attribution.direction_correct = False
        
        # Mock数据库
        mock_db = MagicMock()
        engine = AttributionEngine(db=mock_db)
        
        engine.apply_defensive_update(chain)
        
        # 验证调用了负反馈记录
        mock_db.record_llm_negative_feedback.assert_called_once()
    
    def test_no_defensive_update_on_correct_direction(self):
        """测试方向正确时不触发防御性更新"""
        chain = TradeEvidenceChain()
        chain.evidence.llm_confidence = 0.80
        chain.attribution.direction_correct = True
        
        mock_db = MagicMock()
        engine = AttributionEngine(db=mock_db)
        
        engine.apply_defensive_update(chain)
        
        # 验证没有调用负反馈记录
        mock_db.record_llm_negative_feedback.assert_not_called()
    
    def test_no_defensive_update_low_confidence(self):
        """测试低置信度错误时不触发防御性更新"""
        chain = TradeEvidenceChain()
        chain.evidence.llm_confidence = 0.50  # <0.7阈值
        chain.attribution.direction_correct = False
        
        mock_db = MagicMock()
        engine = AttributionEngine(db=mock_db)
        
        engine.apply_defensive_update(chain)
        
        # 验证没有调用负反馈记录（置信度太低）
        mock_db.record_llm_negative_feedback.assert_not_called()


class TestCausalSummary:
    """测试因果链条摘要生成"""
    
    def test_causal_summary_format(self):
        """测试因果链条摘要格式"""
        chain = TradeEvidenceChain(
            trace_id="TRC-TEST-001",
            symbol="00700",
        )
        chain.evidence.primary_scene = "GAP_EVENT"
        chain.evidence.confidence = 0.85
        chain.evidence.llm_reasoning = "美股昨夜受利好驱动，预计港股开盘存在2%补涨空间"
        chain.evidence.conflict_resolution_log = [
            {"reason": "跨市场套利信号压制趋势信号，因为Z-Score=2.8"}
        ]
        chain.pnl_percent = 2.9
        chain.holding_period_minutes = 45
        chain.attribution.direction_score = 85
        chain.attribution.scene_score = 90
        chain.attribution.execution_score = 88
        chain.feedback.auto_optimization_suggestions = [
            {"suggestion": "仲裁逻辑正确，无需干预"}
        ]
        
        summary = chain.generate_summary()
        
        # 验证包含关键部分
        assert "[交易复盘 TRC-TEST-001]" in summary
        assert "场景识别" in summary
        assert "GAP_EVENT" in summary
        assert "LLM推理" in summary
        assert "实际结果" in summary
        assert "归因分析" in summary
        assert "优化建议" in summary
    
    def test_causal_summary_loss_trade(self):
        """测试亏损交易的因果摘要"""
        chain = TradeEvidenceChain(
            trace_id="TRC-LOSS-001",
            symbol="09988",
        )
        chain.evidence.primary_scene = "TREND_STRONG"
        chain.evidence.confidence = 0.75
        chain.pnl_percent = -3.5
        chain.holding_period_minutes = 120
        chain.attribution.direction_correct = False
        
        summary = chain.generate_summary()
        
        # 验证包含亏损标记
        assert "❌" in summary or "✗" in summary


class TestFullAttributionPipeline:
    """测试完整归因流程"""
    
    def test_attribute_trade_full_pipeline(self):
        """测试完整归因流程"""
        # 构建测试链条
        chain = TradeEvidenceChain(
            trace_id="TRC-FULL-001",
            symbol="00700",
        )
        chain.evidence.primary_scene = "GAP_EVENT"
        chain.evidence.primary_strategy = "confluence_v2"
        chain.evidence.llm_confidence = 0.82
        chain.evidence.llm_reasoning = "看涨"
        chain.pnl_percent = -2.5
        chain.exit_reason = "stop_loss"
        chain.attribution.slippage_bps = 6.0
        
        # Mock数据库
        mock_db = MagicMock()
        mock_db.query_strategy_scene_performance.return_value = {
            'win_rate': 0.25,
            'trade_count': 8,
        }
        
        engine = AttributionEngine(db=mock_db)
        result_chain = engine.attribute_trade(chain)
        
        # 验证归因已完成
        assert len(result_chain.attribution.result_tags) > 0
        assert result_chain.feedback is not None
        assert result_chain.feedback.auto_optimization_suggestions is not None
    
    def test_attribute_trade_with_defensive_update(self):
        """测试归因流程包含防御性更新"""
        chain = TradeEvidenceChain(
            trace_id="TRC-DEF-001",
            symbol="09988",
        )
        chain.evidence.llm_confidence = 0.90  # >0.85阈值
        chain.evidence.llm_reasoning = "强烈看涨"
        chain.pnl_percent = -4.0
        chain.attribution.direction_correct = False
        
        mock_db = MagicMock()
        engine = AttributionEngine(db=mock_db)
        
        result_chain = engine.attribute_trade(chain)
        
        # 验证防御性更新已应用（在attribute_trade中调用apply_defensive_update）
        mock_db.record_llm_negative_feedback.assert_called_once()


class TestWeeklyOptimizer:
    """测试每周优化脚本"""
    
    def test_weekly_optimizer_run(self):
        """测试每周优化器运行"""
        from scripts.weekly_attribution_optimizer import WeeklyAttributionOptimizer
        
        # Mock数据库返回数据
        mock_db = MagicMock()
        mock_db.execute_query.return_value = [
            {
                'trace_id': 'TRC-001',
                'symbol': '00700',
                'strategy_mode': 'confluence_v2',
                'primary_scene': 'GAP_EVENT',
                'pnl': -250.0,
                'pnl_percent': -2.5,
                'direction_correct': False,
                'scene_correct': True,
                'overall_score': 65,
                'result_tags': '["stop_loss_hit"]',
                'slippage_bps': 6.0,
                'llm_confidence': 0.85,
                'trading_session': 'opening',
                'timestamp_open': '2026-04-10T09:45:00',
            },
            {
                'trace_id': 'TRC-002',
                'symbol': '09988',
                'strategy_mode': 'sentiment_ai',
                'primary_scene': 'TREND_STRONG',
                'pnl': 500.0,
                'pnl_percent': 5.0,
                'direction_correct': True,
                'scene_correct': True,
                'overall_score': 90,
                'result_tags': '[]',
                'slippage_bps': 2.0,
                'llm_confidence': 0.75,
                'trading_session': 'morning',
                'timestamp_open': '2026-04-11T10:30:00',
            },
        ]
        
        mock_notifier = MagicMock()
        
        optimizer = WeeklyAttributionOptimizer(db=mock_db, notifier=mock_notifier)
        report = optimizer.run_weekly_optimization()
        
        # 验证报告生成
        assert report is not None
        assert 'report_date' in report
        assert 'total_trades' in report
        assert report['total_trades'] == 2
        
        # 验证钉钉推送被调用
        mock_notifier.send_message.assert_called_once()
    
    def test_weekly_optimizer_no_data(self):
        """测试每周优化器无数据时跳过"""
        from scripts.weekly_attribution_optimizer import WeeklyAttributionOptimizer
        
        mock_db = MagicMock()
        mock_db.execute_query.return_value = []
        
        optimizer = WeeklyAttributionOptimizer(db=mock_db)
        report = optimizer.run_weekly_optimization()
        
        # 无数据时返回None
        assert report is None


# Helper functions
def create_test_chain(**kwargs) -> TradeEvidenceChain:
    """创建测试用的证据链"""
    chain = TradeEvidenceChain()
    
    # 应用自定义参数
    for key, value in kwargs.items():
        if hasattr(chain.evidence, key):
            setattr(chain.evidence, key, value)
        elif hasattr(chain.attribution, key):
            setattr(chain.attribution, key, value)
        elif hasattr(chain, key):
            setattr(chain, key, value)
    
    return chain


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
