"""
智能建议引擎测试
"""
import pytest
import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.review.suggestion_engine import SuggestionEngine
from app.storage.trade_recorder import TradeRecorder


class TestSuggestionEngine:
    """智能建议引擎测试"""
    
    @pytest.fixture
    def setup_low_performance(self):
        """准备低表现测试数据"""
        recorder = TradeRecorder()
        
        # 添加连续亏损交易
        for i in range(5):
            record = recorder.record_open(
                code='00700.HK',
                strategy='test',
                action='BUY',
                entry_price=380.5,
                quantity=100,
                signal_confidence=0.8,
                decision_reason='impulse',  # 冲动交易
                emotion_state='anxious'
            )
            
            # 全部亏损
            recorder.record_close(
                trade_id=record.trade_id,
                exit_price=360.0,
                exit_reason='止损',
                stop_loss_executed=False  # 未执行止损
            )
        
        return recorder
    
    @pytest.fixture
    def setup_high_performance(self):
        """准备高表现测试数据"""
        recorder = TradeRecorder()
        
        # 添加盈利交易
        for i in range(5):
            record = recorder.record_open(
                code='00700.HK',
                strategy='confluence_v2',
                action='BUY',
                entry_price=380.5,
                quantity=100,
                signal_confidence=0.8,
                decision_reason='technical',
                emotion_state='calm'
            )
            
            # 全部盈利
            recorder.record_close(
                trade_id=record.trade_id,
                exit_price=400.0,
                exit_reason='止盈',
                stop_loss_executed=True
            )
        
        return recorder
    
    def test_generate_suggestions_low_performance(self, setup_low_performance):
        """测试低表现时的建议生成"""
        engine = SuggestionEngine()
        suggestions = engine.generate_suggestions(period='last_30_days')
        
        assert len(suggestions) > 0
        
        # 应该有冲动交易警告
        impulse_warnings = [s for s in suggestions if '冲动' in s.get('suggestion', '')]
        assert len(impulse_warnings) > 0
        
        # 应该有止损执行警告
        sl_warnings = [s for s in suggestions if '止损' in s.get('suggestion', '')]
        assert len(sl_warnings) > 0
    
    def test_generate_suggestions_high_performance(self, setup_high_performance):
        """测试高表现时的建议生成"""
        engine = SuggestionEngine()
        suggestions = engine.generate_suggestions(period='last_30_days')
        
        # 高表现时应该有正面反馈
        positive = [s for s in suggestions if s.get('type') == 'positive']
        assert len(positive) > 0
    
    def test_rule_consecutive_losses(self, setup_low_performance):
        """测试连续亏损规则"""
        engine = SuggestionEngine()
        stats = engine._calculate_statistics('last_30_days')
        
        assert stats['max_consecutive_losses'] >= 3
        
        suggestions = engine.generate_suggestions()
        consecutive_warnings = [
            s for s in suggestions 
            if s.get('rule_id') == 'consecutive_losses'
        ]
        assert len(consecutive_warnings) > 0
    
    def test_rule_impulse_ratio(self, setup_low_performance):
        """测试冲动交易规则"""
        engine = SuggestionEngine()
        stats = engine._calculate_statistics('last_30_days')
        
        assert stats['impulse_trade_ratio'] == 1.0  # 100% 冲动
        
        suggestions = engine.generate_suggestions()
        impulse_warnings = [
            s for s in suggestions 
            if s.get('rule_id') == 'impulse_ratio'
        ]
        assert len(impulse_warnings) > 0
    
    def test_rule_stop_loss_execution(self, setup_low_performance):
        """测试止损执行规则"""
        engine = SuggestionEngine()
        stats = engine._calculate_statistics('last_30_days')
        
        assert stats['stop_loss_execution_rate'] == 0.0  # 0% 执行
        
        suggestions = engine.generate_suggestions()
        sl_warnings = [
            s for s in suggestions 
            if s.get('rule_id') == 'stop_loss_execution'
        ]
        assert len(sl_warnings) > 0
    
    def test_rule_low_win_rate(self, setup_low_performance):
        """测试低胜率规则"""
        engine = SuggestionEngine()
        stats = engine._calculate_statistics('last_30_days')
        
        assert stats['win_rate'] == 0.0  # 0% 胜率
        
        suggestions = engine.generate_suggestions()
        winrate_warnings = [
            s for s in suggestions 
            if s.get('rule_id') == 'low_win_rate'
        ]
        assert len(winrate_warnings) > 0
    
    def test_priority_sorting(self, setup_low_performance):
        """测试优先级排序"""
        engine = SuggestionEngine()
        suggestions = engine.generate_suggestions()
        
        # 验证排序 (critical > high > medium > low)
        priority_order = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3}
        priorities = [priority_order.get(s.get('priority', 'low'), 4) for s in suggestions]
        
        assert priorities == sorted(priorities)
    
    def test_positive_feedback(self, setup_high_performance):
        """测试正面反馈"""
        engine = SuggestionEngine()
        suggestions = engine.generate_suggestions()
        
        positive = [s for s in suggestions if s.get('type') == 'positive']
        assert len(positive) > 0
        
        # 应该有胜率相关的正面反馈
        winrate_positive = [
            s for s in positive 
            if '胜率' in s.get('message', '')
        ]
        assert len(winrate_positive) > 0
    
    def test_get_critical_suggestions(self, setup_low_performance):
        """测试获取关键建议"""
        engine = SuggestionEngine()
        critical = engine.get_critical_suggestions()
        
        # 只返回 critical 和 high 优先级
        for s in critical:
            assert s.get('priority') in ['critical', 'high']
    
    def test_get_suggestion_by_category(self, setup_low_performance):
        """测试按类别获取建议"""
        engine = SuggestionEngine()
        
        # 获取风控类建议
        risk_suggestions = engine.get_suggestion_by_category('风控')
        assert len(risk_suggestions) > 0
        
        # 获取心理类建议
        psych_suggestions = engine.get_suggestion_by_category('心理')
        assert len(psych_suggestions) > 0
    
    def test_empty_trades(self):
        """测试无交易情况"""
        engine = SuggestionEngine()
        suggestions = engine.generate_suggestions()
        
        # 无交易时应该有建议 (可能是默认风控建议或正面反馈)
        assert len(suggestions) >= 1
        assert isinstance(suggestions, list)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
