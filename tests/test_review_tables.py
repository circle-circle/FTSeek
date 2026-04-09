"""
复盘数据库表测试
"""
import pytest
import sys
from pathlib import Path
from datetime import datetime
import tempfile
import os

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.storage.review_tables import ReviewDatabase


class TestReviewDatabase:
    """复盘数据库测试"""
    
    @pytest.fixture
    def db(self):
        """创建测试数据库"""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        
        db = ReviewDatabase(db_path)
        yield db
        
        # 清理
        os.unlink(db_path)
    
    def test_init(self, db):
        """测试数据库初始化"""
        assert db.db_path.exists()
    
    def test_add_trade_review(self, db):
        """测试添加交易复盘"""
        trade_data = {
            'id': 'test001',
            'code': '00700.HK',
            'action': 'BUY',
            'entry_price': 380.5,
            'quantity': 100,
            'entry_time': datetime.now().isoformat(),
            'decision_reason': 'technical',
            'decision_confidence': 8,
            'emotion_state': 'calm'
        }
        
        trade_id = db.add_trade_review(trade_data)
        assert trade_id == 'test001'
        
        # 验证读取
        result = db.get_trade_review('test001')
        assert result is not None
        assert result['code'] == '00700.HK'
        assert result['decision_confidence'] == 8
    
    def test_update_trade_review(self, db):
        """测试更新交易复盘"""
        # 先添加
        db.add_trade_review({
            'id': 'test002',
            'code': '00700.HK',
            'action': 'BUY',
            'entry_price': 380.5,
            'quantity': 100,
            'entry_time': datetime.now().isoformat()
        })
        
        # 更新
        success = db.update_trade_review('test002', {
            'exit_price': 400.0,
            'pnl': 1950.0,
            'exit_timing_score': 7
        })
        
        assert success is True
        
        # 验证
        result = db.get_trade_review('test002')
        assert result['exit_price'] == 400.0
        assert result['exit_timing_score'] == 7
    
    def test_add_review_summary(self, db):
        """测试添加复盘总结"""
        review_data = {
            'id': 'review001',
            'trade_id': 'test001',
            'review_date': datetime.now().strftime('%Y-%m-%d'),
            'overall_score': 8,
            'decision_score': 7,
            'what_went_well': '严格执行策略',
            'what_went_wrong': '入场稍早'
        }
        
        review_id = db.add_review_summary(review_data)
        assert review_id == 'review001'
        
        result = db.get_review_summary('test001')
        assert result is not None
        assert result['overall_score'] == 8
    
    def test_save_strategy_performance(self, db):
        """测试保存策略表现"""
        perf_data = {
            'id': 'perf001',
            'strategy_name': 'confluence_v2',
            'period_start': '2026-03-01',
            'period_end': '2026-03-31',
            'total_trades': 20,
            'winning_trades': 12,
            'total_pnl': 15000.0,
            'win_rate': 0.6
        }
        
        perf_id = db.save_strategy_performance(perf_data)
        assert perf_id == 'perf001'
    
    def test_add_error_pattern(self, db):
        """测试添加错误模式"""
        pattern_data = {
            'id': 'pattern001',
            'pattern_name': '冲动交易',
            'pattern_category': '决策',
            'description': '未经充分分析就入场',
            'occurrence_count': 5,
            'severity_level': 'high'
        }
        
        pattern_id = db.add_error_pattern(pattern_data)
        assert pattern_id == 'pattern001'
        
        # 查询
        patterns = db.get_error_patterns(category='决策')
        assert len(patterns) > 0
    
    def test_save_psychology_stats(self, db):
        """测试保存心理统计"""
        stats_data = {
            'id': 'psych001',
            'trader_id': 'user_001',
            'stat_date': datetime.now().strftime('%Y-%m-%d'),
            'calm_count': 10,
            'anxious_count': 3,
            'total_trades': 15,
            'avg_emotion_intensity': 3.5
        }
        
        stats_id = db.save_psychology_stats(stats_data)
        assert stats_id == 'psych001'
        
        result = db.get_psychology_stats('user_001', '2026-03-01', '2026-03-31')
        assert len(result) > 0
    
    def test_add_improvement_action(self, db):
        """测试添加改进行动"""
        action_data = {
            'id': 'action001',
            'trader_id': 'user_001',
            'action_title': '减少冲动交易',
            'action_category': '决策',
            'priority': 'high',
            'status': 'pending',
            'created_date': datetime.now().strftime('%Y-%m-%d')
        }
        
        action_id = db.add_improvement_action(action_data)
        assert action_id == 'action001'
        
        # 查询待处理
        actions = db.get_pending_actions('user_001')
        assert len(actions) > 0
        
        # 更新状态
        success = db.update_action_status('action001', 'completed')
        assert success is True
    
    def test_record_best_worst_trade(self, db):
        """测试记录最佳/最差交易"""
        data = {
            'id': 'best001',
            'trade_id': 'test001',
            'record_type': 'best',
            'period': 'weekly',
            'code': '00700.HK',
            'action': 'BUY',
            'entry_price': 100.0,
            'exit_price': 150.0,
            'pnl': 5000.0,
            'pnl_percent': 50.0,
            'highlight': '精准把握入场时机',
            'key_factor': '技术面突破',
            'record_date': datetime.now().strftime('%Y-%m-%d')
        }
        
        record_id = db.record_best_worst_trade(data)
        assert record_id == 'best001'
        
        result = db.get_best_worst_trades('weekly', 'best')
        assert len(result) > 0
    
    def test_save_sim_real_comparison(self, db):
        """测试保存模拟 vs 实盘对比"""
        data = {
            'id': 'comp001',
            'period_start': '2026-03-01',
            'period_end': '2026-03-31',
            'sim_total_trades': 20,
            'sim_win_rate': 0.65,
            'real_total_trades': 15,
            'real_win_rate': 0.55,
            'win_rate_diff': 0.10
        }
        
        record_id = db.save_sim_real_comparison(data)
        assert record_id == 'comp001'
        
        result = db.get_sim_real_comparison('2026-03-01', '2026-03-31')
        assert result is not None
        assert result['win_rate_diff'] == 0.10
    
    def test_get_review_statistics(self, db):
        """测试获取复盘统计"""
        # 添加测试数据
        for i in range(5):
            db.add_trade_review({
                'id': f'stat{i}',
                'code': '00700.HK',
                'action': 'BUY',
                'entry_price': 380.5,
                'quantity': 100,
                'entry_time': '2026-03-15T10:00:00',
                'pnl': 1000.0 if i < 3 else -500.0,
                'emotion_state': 'calm' if i < 4 else 'anxious'
            })
        
        stats = db.get_review_statistics('2026-03-01', '2026-03-31')
        
        assert stats['total_trades'] == 5
        assert stats['winning_trades'] == 3
        assert stats['losing_trades'] == 2
        assert 'emotion_distribution' in stats


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
