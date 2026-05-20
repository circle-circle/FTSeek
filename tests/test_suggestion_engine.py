"""
智能建议引擎测试 (美股版)
"""
import pytest
import sys
from pathlib import Path
import os

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.review.suggestion_engine import SuggestionEngine
from app.storage.trade_recorder import TradeRecorder


class TestSuggestionEngine:
    """智能建议引擎测试 (美股版)"""
    
    @pytest.fixture
    def setup_low_performance(self):
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
                decision_reason='impulse'
            )
            
            recorder.record_close(
                trade_id=trade_id,
                exit_price=170.0,
                exit_reason='SL',
                stop_loss_executed=False
            )
        
        yield recorder
        os.unlink(db_path)
    
    def test_generate_suggestions(self, setup_low_performance):
        engine = SuggestionEngine()
        suggestions = engine.generate_suggestions(period='last_30_days')
        
        assert len(suggestions) > 0
    
    def test_rule_consecutive_losses(self, setup_low_performance):
        engine = SuggestionEngine()
        stats = engine._calculate_stats('last_30_days')
        
        assert stats['max_consecutive_losses'] >= 3
        
        suggestions = engine.generate_suggestions()
        consecutive_warnings = [
            s for s in suggestions 
            if s.get('rule_id') == 'consecutive_losses'
        ]
        assert len(consecutive_warnings) > 0
    
    def test_rule_stop_loss_execution(self, setup_low_performance):
        engine = SuggestionEngine()
        stats = engine._calculate_stats('last_30_days')
        
        assert stats['stop_loss_rate'] == 0.0
        
        suggestions = engine.generate_suggestions()
        sl_warnings = [
            s for s in suggestions 
            if s.get('rule_id') == 'stop_loss_execution'
        ]
        assert len(sl_warnings) > 0
    
    def test_priority_sorting(self, setup_low_performance):
        engine = SuggestionEngine()
        suggestions = engine.generate_suggestions()
        
        priority_order = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3}
        priorities = [priority_order.get(s.get('priority', 'low'), 4) for s in suggestions]
        
        assert priorities == sorted(priorities)
    
    def test_get_critical_suggestions(self, setup_low_performance):
        engine = SuggestionEngine()
        critical = engine.get_critical_suggestions()
        
        for s in critical:
            assert s.get('priority') in ['critical', 'high']


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
