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
        import app.storage.trade_recorder as tr_mod

        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        
        recorder = TradeRecorder(db_path)
        
        for i in range(5):
            trade_id = recorder.record(
                stock_code='AAPL',
                side='buy',
                quantity=100,
                price=175.5,
                decision_reason='impulse',
                stop_loss=172.0,
            )
            
            recorder.record_close(
                trade_id=trade_id,
                exit_price=170.0,
                exit_reason='SL',
                stop_loss_executed=False
            )
        
        # SuggestionEngine 经 get_trade_recorder() 单例取 recorder —
        # 密封: 把单例指到临时 DB, 避免读真实 trade_records.db
        _old_singleton = tr_mod._recorder
        tr_mod._recorder = recorder
        yield recorder
        tr_mod._recorder = _old_singleton
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
    
    def test_stop_loss_execution_rule_removed(self, setup_low_performance):
        """2026-11-01 拆除: 判据读 stop_loss_planned/executed, 活跃库无此两列 ⇒ 恒默认 1.0。

        本条原为 test_rule_stop_loss_execution, 用本模块自建临时库 + stop_loss_executed=False
        让规则"触发"。但生产在读的活跃库 ftseek_us.db 没有这两列 (PRAGMA 实测 52 列),
        本模块亦无任何调用方 ⇒ 该断言守的是一条永远跑不到的生产路径, 属假绿。
        改为钉"规则必须不再存在", 与 tests/review/test_dead_review_patterns.py 同范式。
        """
        engine = SuggestionEngine()
        stats = engine._calculate_stats('last_30_days')

        assert 'stop_loss_rate' not in stats, "伪指标不得留在 stats 里"
        assert 'stop_loss_execution' not in [r['id'] for r in engine.RULES], (
            "stop_loss_execution 规则读活跃库不存在的列 ⇒ 永不触发; 真接线前不得复活")

        # 反向: 其余规则仍须正常工作 (不是把整块删光)
        assert engine.generate_suggestions()
    
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
