"""
每日复盘生成器测试
"""
import pytest
import sys
from pathlib import Path
from datetime import datetime
import tempfile
import os

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.review.daily_review import DailyReviewGenerator
from app.storage.trade_recorder import TradeRecorder


class TestDailyReviewGenerator:
    """每日复盘生成器测试"""
    
    @pytest.fixture
    def setup_data(self):
        """准备测试数据"""
        recorder = TradeRecorder()
        
        # 添加测试交易
        for i in range(5):
            record = recorder.record_open(
                code='00700.HK',
                strategy='confluence_v2',
                action='BUY',
                entry_price=380.5,
                quantity=100,
                signal_confidence=0.8,
                decision_reason='technical' if i < 3 else 'impulse',
                decision_confidence=8 if i < 3 else 4,
                emotion_state='calm' if i < 4 else 'anxious'
            )
            
            # 平仓
            recorder.record_close(
                trade_id=record.trade_id,
                exit_price=400.0 if i < 3 else 360.0,
                exit_reason='止盈' if i < 3 else '止损',
                exit_timing_score=7 if i < 3 else 4
            )
        
        return recorder
    
    def test_generate(self, setup_data):
        """测试生成每日复盘"""
        generator = DailyReviewGenerator()
        today = datetime.now().strftime('%Y-%m-%d')
        
        report = generator.generate(date=today)
        
        assert report is not None
        assert report['report_type'] == 'daily'
        assert report['report_date'] == today
        assert 'summary' in report
        assert 'decision_analysis' in report
        assert 'psychology_analysis' in report
    
    def test_summary_generation(self, setup_data):
        """测试摘要生成"""
        generator = DailyReviewGenerator()
        today = datetime.now().strftime('%Y-%m-%d')
        
        report = generator.generate(date=today)
        summary = report['summary']
        
        assert summary['total_trades'] == 5
        assert summary['closed_trades'] == 5
        assert summary['winning_trades'] == 3
        assert summary['losing_trades'] == 2
        assert 'win_rate' in summary
    
    def test_decision_analysis(self, setup_data):
        """测试决策分析"""
        generator = DailyReviewGenerator()
        today = datetime.now().strftime('%Y-%m-%d')
        
        report = generator.generate(date=today)
        decision = report['decision_analysis']
        
        assert 'decision_reason_distribution' in decision
        assert 'impulse_trade_count' in decision
        assert decision['impulse_trade_count'] == 2
    
    def test_psychology_analysis(self, setup_data):
        """测试心理分析"""
        generator = DailyReviewGenerator()
        today = datetime.now().strftime('%Y-%m-%d')
        
        report = generator.generate(date=today)
        psychology = report['psychology_analysis']
        
        assert 'emotion_distribution' in psychology
        assert 'calm' in psychology['emotion_distribution']
        assert 'anxious' in psychology['emotion_distribution']
    
    def test_suggestions_generation(self, setup_data):
        """测试建议生成"""
        generator = DailyReviewGenerator()
        today = datetime.now().strftime('%Y-%m-%d')
        
        report = generator.generate(date=today)
        suggestions = report['improvement_suggestions']
        
        assert isinstance(suggestions, list)
        # 由于有冲动交易，应该有相关建议
        assert len(suggestions) > 0
    
    def test_best_worst_trade(self, setup_data):
        """测试最佳/最差交易识别"""
        generator = DailyReviewGenerator()
        today = datetime.now().strftime('%Y-%m-%d')
        
        report = generator.generate(date=today)
        
        assert 'best_trade' in report
        assert 'worst_trade' in report
        
        if report['best_trade']:
            assert 'trade_id' in report['best_trade']
            assert 'pnl' in report['best_trade']
    
    def test_save_report(self, setup_data):
        """测试报告保存"""
        generator = DailyReviewGenerator()
        today = datetime.now().strftime('%Y-%m-%d')
        
        generator.generate(date=today)
        
        # 验证文件存在
        filename = generator.output_dir / f"daily_review_{today}.json"
        assert filename.exists()
    
    def test_get_report(self, setup_data):
        """测试获取已保存报告"""
        generator = DailyReviewGenerator()
        today = datetime.now().strftime('%Y-%m-%d')
        
        generator.generate(date=today)
        
        # 读取
        report = generator.get_report(today)
        assert report is not None
        assert report['report_type'] == 'daily'
    
    def test_empty_trades(self):
        """测试无交易情况"""
        generator = DailyReviewGenerator()
        # 使用过去日期，确保无交易
        report = generator.generate(date='2020-01-01')
        
        assert report is not None
        assert report['summary']['closed_trades'] == 0


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
