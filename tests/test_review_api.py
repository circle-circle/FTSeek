"""
复盘系统 API 接口测试

测试覆盖:
- 交易记录创建/更新
- 复盘报告查询
- 错误模式分析
- 改进建议查询
- 模拟 vs 实盘对比
- 策略表现分析
- 心理分析
"""
import pytest
import json
from datetime import datetime, timedelta
from unittest.mock import Mock, patch, MagicMock


class TestTradeAPI:
    """交易记录 API 测试"""
    
    def test_create_trade_success(self, client):
        """测试创建交易记录成功"""
        trade_data = {
            'stock_code': 'AAPL',
            'side': 'buy',
            'quantity': 100,
            'price': 150.50,
            'trade_time': '2026-03-28T10:30:00',
            'decision_reason': 'technical_breakout',
            'decision_confidence': 8,
            'emotion_state': 'calm',
            'emotion_intensity': 3,
            'account_type': 'paper'
        }
        
        with patch('app.storage.trade_recorder.TradeRecorder') as mock_recorder:
            mock_recorder.return_value.record.return_value = 'trade_test123'
            
            response = client.post(
                '/api/review/trades',
                data=json.dumps(trade_data),
                content_type='application/json'
            )
            
            assert response.status_code == 201
            data = json.loads(response.data)
            assert data['status'] == 'success'
            assert data['trade_id'] == 'trade_test123'
    
    def test_create_trade_missing_fields(self, client):
        """测试创建交易记录缺少必填字段"""
        trade_data = {
            'stock_code': 'AAPL',
            # 缺少 side, quantity, price, trade_time
        }
        
        response = client.post(
            '/api/review/trades',
            data=json.dumps(trade_data),
            content_type='application/json'
        )
        
        assert response.status_code == 400
        data = json.loads(response.data)
        assert 'error' in data
        assert '缺少必填字段' in data['error']
    
    def test_update_trade_review(self, client):
        """测试更新交易复盘信息"""
        review_data = {
            'entry_timing_score': 7,
            'exit_timing_score': 5,
            'stop_loss_executed': True,
            'key_learning': '突破确认后再入场更稳妥'
        }
        
        with patch('app.storage.trade_recorder.TradeRecorder') as mock_recorder:
            mock_recorder.return_value.update_review.return_value = None
            
            response = client.put(
                '/api/review/trades/trade_test123/review',
                data=json.dumps(review_data),
                content_type='application/json'
            )
            
            assert response.status_code == 200
            data = json.loads(response.data)
            assert data['status'] == 'success'


class TestReviewReportAPI:
    """复盘报告 API 测试"""
    
    def test_get_daily_report(self, client):
        """测试获取每日复盘报告"""
        with patch('app.review.daily_review.DailyReviewGenerator') as mock_generator:
            mock_gen_instance = Mock()
            mock_gen_instance.generate.return_value = {
                'report_type': 'daily',
                'date': '2026-03-28',
                'stats': {
                    'total_trades': 5,
                    'win_rate': 60.0,
                    'total_return': 2.5
                },
                'dimensions': {
                    'decision': 8,
                    'timing': 6,
                    'position': 8,
                    'risk': 4,
                    'psychology': 6,
                    'strategy': 8
                }
            }
            mock_generator.return_value = mock_gen_instance
            
            response = client.get('/api/review/report?type=daily&date=2026-03-28')
            
            assert response.status_code == 200
            data = json.loads(response.data)
            assert data['report_type'] == 'daily'
            assert data['date'] == '2026-03-28'
            assert 'stats' in data
            assert 'dimensions' in data
    
    def test_get_weekly_report(self, client):
        """测试获取每周复盘报告"""
        with patch('app.review.weekly_review.WeeklyReviewGenerator') as mock_generator:
            mock_gen_instance = Mock()
            mock_gen_instance.generate.return_value = {
                'report_type': 'weekly',
                'week_start': '2026-03-24',
                'weekly_summary': '本周交易 15 笔，胜率 58%'
            }
            mock_generator.return_value = mock_gen_instance
            
            response = client.get('/api/review/report?type=weekly&date=2026-03-24')
            
            assert response.status_code == 200
            data = json.loads(response.data)
            assert data['report_type'] == 'weekly'
    
    def test_get_report_invalid_type(self, client):
        """测试获取无效类型的报告"""
        response = client.get('/api/review/report?type=invalid')
        
        assert response.status_code == 400
        data = json.loads(response.data)
        assert 'error' in data


class TestErrorPatternsAPI:
    """错误模式 API 测试"""
    
    def test_get_error_patterns(self, client):
        """测试获取错误模式分析"""
        with patch('app.review.error_pattern_detector.ErrorPatternDetector') as mock_detector:
            mock_detector_instance = Mock()
            mock_detector_instance.detect.return_value = [
                {
                    'type': 'stop_loss_not_executed',
                    'name': '止损不执行',
                    'count': 8,
                    'trend': 'up',
                    'impact': '$1,240',
                    'suggestion': '设置止损单'
                }
            ]
            mock_detector_instance.get_trend.return_value = {
                'weekly': [5, 4, 6, 8]
            }
            mock_detector.return_value = mock_detector_instance
            
            with patch('app.storage.trade_recorder.TradeRecorder') as mock_recorder:
                mock_recorder.return_value.get_improvement_actions.return_value = []
                
                response = client.get('/api/review/error-patterns?period=30d')
                
                assert response.status_code == 200
                data = json.loads(response.data)
                assert 'patterns' in data
                assert len(data['patterns']) > 0
                assert 'trend' in data


class TestSuggestionsAPI:
    """改进建议 API 测试"""
    
    def test_get_suggestions(self, client):
        """测试获取改进建议"""
        with patch('app.review.suggestion_engine.SuggestionEngine') as mock_engine:
            mock_engine_instance = Mock()
            mock_engine_instance.get_suggestions.return_value = [
                {
                    'id': 'suggestion_001',
                    'priority': 'P0',
                    'suggestion': '考虑暂停交易，调整心态',
                    'reason': '连续亏损 >= 3 笔',
                    'rule_id': 'rule_001'
                }
            ]
            mock_engine.return_value = mock_engine_instance
            
            response = client.get('/api/review/suggestions?limit=10')
            
            assert response.status_code == 200
            data = json.loads(response.data)
            assert 'suggestions' in data
            assert len(data['suggestions']) > 0


class TestSimRealCompareAPI:
    """模拟 vs 实盘对比 API 测试"""
    
    def test_get_sim_real_compare(self, client):
        """测试获取模拟 vs 实盘对比数据"""
        response = client.get('/api/review/sim-real-compare?period=30d')
        
        assert response.status_code == 200
        data = json.loads(response.data)
        assert 'metrics' in data
        assert 'paper' in data['metrics']
        assert 'real' in data['metrics']
        assert 'equity_curves' in data
        assert 'holdings' in data
        assert 'analysis' in data


class TestStrategyPerformanceAPI:
    """策略表现 API 测试"""
    
    def test_get_strategy_performance(self, client):
        """测试获取策略表现分析"""
        with patch('app.review.strategy_analysis.StrategyAnalyzer') as mock_analyzer:
            mock_analyzer_instance = Mock()
            mock_analyzer_instance.analyze_strategy.return_value = {
                'total_trades': 45,
                'win_rate': 58.0,
                'avg_return': 2.3,
                'max_drawdown': -4.5,
                'sharpe_ratio': 1.8
            }
            mock_analyzer.return_value = mock_analyzer_instance
            
            response = client.get('/api/review/strategy/ma_cross_v2?start_date=2026-01-01&end_date=2026-03-28')
            
            assert response.status_code == 200
            data = json.loads(response.data)
            assert data['strategy_name'] == 'ma_cross_v2'
            assert 'stats' in data
            assert data['stats']['win_rate'] == 58.0


class TestPsychologyAnalysisAPI:
    """心理分析 API 测试"""
    
    def test_get_psychology_analysis(self, client):
        """测试获取心理状态分析"""
        with patch('app.review.psychology_analyzer.PsychologyAnalyzer') as mock_analyzer:
            mock_analyzer_instance = Mock()
            mock_analyzer_instance.analyze.return_value = {
                'emotion_distribution': {
                    'calm': 45,
                    'nervous': 20,
                    'excited': 15
                },
                'emotion_performance': {
                    'calm': {'win_rate': 65, 'avg_return': 2.5},
                    'nervous': {'win_rate': 45, 'avg_return': -1.2}
                }
            }
            mock_analyzer.return_value = mock_analyzer_instance
            
            response = client.get('/api/review/psychology?period=30d')
            
            assert response.status_code == 200
            data = json.loads(response.data)
            assert 'emotion_distribution' in data
            assert 'emotion_performance' in data


class TestHealthCheckAPI:
    """健康检查 API 测试"""
    
    def test_health_check(self, client):
        """测试健康检查接口"""
        response = client.get('/api/review/health')
        
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data['status'] == 'healthy'
        assert 'timestamp' in data


# ==================== Fixtures ====================

@pytest.fixture
def client():
    """创建测试客户端"""
    from flask import Flask
    from app.review.review_api import review_api
    
    app = Flask(__name__)
    app.register_blueprint(review_api)
    app.config['TESTING'] = True
    
    with app.test_client() as client:
        yield client


# ==================== 运行测试 ====================

if __name__ == '__main__':
    pytest.main([__file__, '-v', '--cov=app/review/review_api', '--cov-report=html'])
