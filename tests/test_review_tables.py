"""
复盘数据库表测试 (美股版)
"""
import pytest
import sys
from pathlib import Path
from datetime import datetime
import tempfile
import os

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.storage.review_tables import ReviewDatabase


class TestReviewDatabase:
    """复盘数据库测试 (美股版)"""
    
    @pytest.fixture
    def db(self):
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        db = ReviewDatabase(db_path)
        yield db
        os.unlink(db_path)
    
    def test_init(self, db):
        assert db.db_path.exists()
    
    def test_add_trade_review(self, db):
        trade_data = {
            'id': 'test001',
            'code': 'AAPL',
            'action': 'LONG',
            'entry_price': 175.5,
            'quantity': 100,
            'entry_time': datetime.now().isoformat(),
            'decision_reason': 'technical',
            'decision_confidence': 8
        }
        
        trade_id = db.add_trade_review(trade_data)
        assert trade_id == 'test001'
        
        result = db.get_trade_review('test001')
        assert result is not None
        assert result['code'] == 'AAPL'
    
    def test_update_trade_review(self, db):
        db.add_trade_review({
            'id': 'test002',
            'code': 'AAPL',
            'action': 'LONG',
            'entry_price': 175.5,
            'quantity': 100,
            'entry_time': datetime.now().isoformat()
        })
        
        success = db.update_trade_review('test002', {
            'exit_price': 180.0,
            'pnl': 450.0
        })
        
        assert success is True
        result = db.get_trade_review('test002')
        assert result['exit_price'] == 180.0
    
    def test_add_review_summary(self, db):
        review_data = {
            'id': 'review001',
            'trade_id': 'test001',
            'review_date': datetime.now().strftime('%Y-%m-%d'),
            'overall_score': 8
        }
        
        review_id = db.add_review_summary(review_data)
        assert review_id == 'review001'
    
    def test_save_strategy_performance(self, db):
        perf_data = {
            'id': 'perf001',
            'strategy_name': 'confluence_v2',
            'period_start': '2026-03-01',
            'period_end': '2026-03-31',
            'total_trades': 20,
            'win_rate': 0.6
        }
        
        perf_id = db.save_strategy_performance(perf_data)
        assert perf_id == 'perf001'
    
    def test_add_error_pattern(self, db):
        pattern_data = {
            'id': 'pattern001',
            'pattern_name': 'Impulse Trading',
            'pattern_category': '决策',
            'occurrence_count': 5,
            'severity_level': 'high'
        }
        
        pattern_id = db.add_error_pattern(pattern_data)
        assert pattern_id == 'pattern001'
        
        patterns = db.get_error_patterns(category='决策')
        assert len(patterns) > 0
    
    def test_save_psychology_stats(self, db):
        stats_data = {
            'id': 'psych001',
            'trader_id': 'user_001',
            'stat_date': datetime.now().strftime('%Y-%m-%d'),
            'total_trades': 15
        }
        
        stats_id = db.save_psychology_stats(stats_data)
        assert stats_id == 'psych001'
    
    def test_add_improvement_action(self, db):
        action_data = {
            'id': 'action001',
            'trader_id': 'user_001',
            'action_title': 'Reduce Impulse Trading',
            'action_category': '决策',
            'priority': 'high',
            'status': 'pending',
            'created_date': datetime.now().strftime('%Y-%m-%d')
        }
        
        action_id = db.add_improvement_action(action_data)
        assert action_id == 'action001'
        
        actions = db.get_pending_actions('user_001')
        assert len(actions) > 0
    
    def test_get_review_statistics(self, db):
        for i in range(5):
            db.add_trade_review({
                'id': f'stat{i}',
                'code': 'AAPL',
                'action': 'LONG',
                'entry_price': 175.5,
                'quantity': 100,
                'entry_time': '2026-03-15T10:00:00',
                'pnl': 1000.0 if i < 3 else -500.0
            })
        
        stats = db.get_review_statistics('2026-03-01', '2026-03-31')
        
        assert stats['total_trades'] == 5
        assert stats['winning_trades'] == 3


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
