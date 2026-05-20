"""
每日复盘生成器测试 (美股版)
"""
import pytest
import sys
from pathlib import Path
from datetime import datetime
import os

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.review.daily_review import DailyReviewGenerator
from app.storage.trade_recorder import TradeRecorder


class TestDailyReviewGenerator:
    """每日复盘生成器测试 (美股版)"""
    
    @pytest.fixture
    def setup_data(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        
        recorder = TradeRecorder(db_path)
        
        for i in range(5):
            trade_id = recorder.record(
                stock_code='AAPL',
                side='buy',
                quantity=100,
                price=175.5,
                decision_reason='technical' if i < 3 else 'impulse',
                emotion_state='calm' if i < 4 else 'anxious'
            )
            
            recorder.record_close(
                trade_id=trade_id,
                exit_price=180.0 if i < 3 else 170.0,
                exit_reason='TP' if i < 3 else 'SL'
            )
        
        yield recorder
        os.unlink(db_path)
    
    def test_generate(self, setup_data):
        generator = DailyReviewGenerator()
        today = datetime.now().strftime('%Y-%m-%d')
        
        report = generator.generate(date=today)
        
        assert report is not None
        assert report['report_type'] == 'daily'
        assert 'summary' in report
    
    def test_summary_generation(self, setup_data):
        generator = DailyReviewGenerator()
        today = datetime.now().strftime('%Y-%m-%d')
        
        report = generator.generate(date=today)
        summary = report['summary']
        
        assert summary['total_trades'] == 5
        assert summary['closed_trades'] == 5
        assert summary['winning_trades'] == 3
    
    def test_suggestions_generation(self, setup_data):
        generator = DailyReviewGenerator()
        today = datetime.now().strftime('%Y-%m-%d')
        
        report = generator.generate(date=today)
        suggestions = report['improvement_suggestions']
        
        assert isinstance(suggestions, list)
    
    def test_save_report(self, setup_data):
        generator = DailyReviewGenerator()
        today = datetime.now().strftime('%Y-%m-%d')
        
        generator.generate(date=today)
        
        filename = generator.output_dir / f"daily_review_{today}.json"
        assert filename.exists()


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
