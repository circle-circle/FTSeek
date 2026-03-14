"""
测试精简 API 端点 (Task 10)

测试 4 个核心 API:
1. GET /api/v2/positions - 查询所有持仓
2. GET /api/v2/performance - 查询绩效统计
3. POST /api/v2/mode/{code} - 切换交易模式
4. POST /api/v2/manual_exit/{code} - 手动平仓
"""
import pytest
import sys
import os
import json
from unittest.mock import Mock, patch, MagicMock

# 添加项目路径
sys.path.insert(0, '/opt/ftseek/FTSeek')

from app.v4.api.decision_api import create_app, _check_api_key


class TestAPIKeyValidation:
    """测试 API Key 认证"""
    
    def test_check_api_key_valid(self):
        """测试有效的 API Key"""
        with patch('app.v4.api.decision_api.API_KEY', 'test-key-123'):
            # 模拟 Flask 请求
            mock_request = Mock()
            mock_request.headers = {'X-API-Key': 'test-key-123'}
            
            with patch('app.v4.api.decision_api.request', mock_request):
                # 应该返回 None 表示通过认证
                result = _check_api_key()
                assert result is None
    
    def test_check_api_key_invalid(self):
        """测试无效的 API Key"""
        with patch('app.v4.api.decision_api.API_KEY', 'test-key-123'):
            mock_request = Mock()
            mock_request.headers = {'X-API-Key': 'wrong-key'}
            
            with patch('app.v4.api.decision_api.request', mock_request):
                result = _check_api_key()
                # 应该返回错误元组
                assert result is not None
                error_msg, status_code = result
                assert status_code == 401
                assert 'Invalid' in error_msg
    
    def test_check_api_key_missing(self):
        """测试缺失 API Key"""
        with patch('app.v4.api.decision_api.API_KEY', 'test-key-123'):
            mock_request = Mock()
            mock_request.headers = {}
            
            with patch('app.v4.api.decision_api.request', mock_request):
                result = _check_api_key()
                # 应该返回错误元组
                assert result is not None
                error_msg, status_code = result
                assert status_code == 401
                assert 'Missing' in error_msg


class TestPositionsAPI:
    """测试持仓查询 API"""
    
    @pytest.fixture
    def client(self):
        """创建测试客户端"""
        with patch('app.v4.api.decision_api.API_KEY', 'test-key'):
            app = create_app(testing=True)
            app.config['TESTING'] = True
            with app.test_client() as client:
                yield client
    
    def test_get_positions_success(self, client):
        """测试成功获取持仓"""
        mock_positions = [
            {
                'code': '0700.HK',
                'quantity': 100,
                'avg_cost': 382.40,
                'current_price': 401.52,
                'pnl': 1912.00,
                'pnl_pct': 0.05
            }
        ]
        
        with patch('app.v4.api.decision_api.DecisionEngine') as mock_engine_class:
            mock_engine = Mock()
            mock_engine.get_all_positions.return_value = mock_positions
            mock_engine_class.get_instance.return_value = mock_engine
            
            response = client.get(
                '/api/v2/positions',
                headers={'X-API-Key': 'test-key'}
            )
            
            assert response.status_code == 200
            data = json.loads(response.data)
            assert 'positions' in data
            assert 'total_count' in data
            assert data['total_count'] == 1
            assert data['positions'][0]['code'] == '0700.HK'
    
    def test_get_positions_empty(self, client):
        """测试无持仓情况"""
        with patch('app.v4.api.decision_api.DecisionEngine') as mock_engine_class:
            mock_engine = Mock()
            mock_engine.get_all_positions.return_value = []
            mock_engine_class.get_instance.return_value = mock_engine
            
            response = client.get(
                '/api/v2/positions',
                headers={'X-API-Key': 'test-key'}
            )
            
            assert response.status_code == 200
            data = json.loads(response.data)
            assert data['positions'] == []
            assert data['total_count'] == 0
    
    def test_get_positions_auth_required(self, client):
        """测试需要认证"""
        response = client.get('/api/v2/positions')
        assert response.status_code == 401


class TestPerformanceAPI:
    """测试绩效统计 API"""
    
    @pytest.fixture
    def client(self):
        """创建测试客户端"""
        with patch('app.v4.api.decision_api.API_KEY', 'test-key'):
            app = create_app(testing=True)
            app.config['TESTING'] = True
            with app.test_client() as client:
                yield client
    
    def test_get_performance_success(self, client):
        """测试成功获取绩效统计"""
        mock_stats = {
            'code': '0700.HK',
            'total_trades': 12,
            'winning_trades': 8,
            'win_rate': 0.67,
            'total_pnl': 8500.00,
            'total_return': 0.085,
            'max_drawdown': -0.042
        }
        
        with patch('app.v4.api.decision_api.PerformanceTracker') as mock_tracker_class:
            mock_tracker = Mock()
            mock_tracker.get_performance_stats.return_value = mock_stats
            mock_tracker_class.return_value = mock_tracker
            
            response = client.get(
                '/api/v2/performance?code=0700.HK&days=30',
                headers={'X-API-Key': 'test-key'}
            )
            
            assert response.status_code == 200
            data = json.loads(response.data)
            assert data['code'] == '0700.HK'
            assert data['total_trades'] == 12
            assert data['win_rate'] == 0.67
    
    def test_get_performance_default_days(self, client):
        """测试默认天数参数"""
        with patch('app.v4.api.decision_api.PerformanceTracker') as mock_tracker_class:
            mock_tracker = Mock()
            mock_tracker.get_performance_stats.return_value = {'code': '0700.HK'}
            mock_tracker_class.return_value = mock_tracker
            
            response = client.get(
                '/api/v2/performance?code=0700.HK',
                headers={'X-API-Key': 'test-key'}
            )
            
            assert response.status_code == 200
            # 验证默认使用 30 天
            mock_tracker.get_performance_stats.assert_called_with('0700.HK', days=30)
    
    def test_get_performance_missing_code(self, client):
        """测试缺少 code 参数"""
        response = client.get(
            '/api/v2/performance?days=30',
            headers={'X-API-Key': 'test-key'}
        )
        assert response.status_code == 400


class TestModeAPI:
    """测试交易模式切换 API"""
    
    @pytest.fixture
    def client(self):
        """创建测试客户端"""
        with patch('app.v4.api.decision_api.API_KEY', 'test-key'):
            app = create_app(testing=True)
            app.config['TESTING'] = True
            with app.test_client() as client:
                yield client
    
    def test_switch_mode_success(self, client):
        """测试成功切换模式"""
        with patch('app.v4.api.decision_api.DecisionEngine') as mock_engine_class:
            mock_engine = Mock()
            mock_engine.get_mode.return_value = 'HYBRID'
            mock_engine.set_mode.return_value = None
            
            mock_engine_class.get_instance.return_value = mock_engine
            
            response = client.post(
                '/api/v2/mode/0700.HK',
                headers={
                    'X-API-Key': 'test-key',
                    'Content-Type': 'application/json'
                },
                data=json.dumps({'mode': 'PAPER'})
            )
            
            assert response.status_code == 200
            data = json.loads(response.data)
            assert data['code'] == '0700.HK'
            assert data['old_mode'] == 'HYBRID'
            assert data['new_mode'] == 'PAPER'
            assert 'timestamp' in data
    
    def test_switch_mode_invalid_mode(self, client):
        """测试无效模式"""
        response = client.post(
            '/api/v2/mode/0700.HK',
            headers={
                'X-API-Key': 'test-key',
                'Content-Type': 'application/json'
            },
            data=json.dumps({'mode': 'INVALID'})
        )
        assert response.status_code == 400
    
    def test_switch_mode_missing_body(self, client):
        """测试缺少请求体"""
        response = client.post(
            '/api/v2/mode/0700.HK',
            headers={'X-API-Key': 'test-key'}
        )
        assert response.status_code == 400


class TestManualExitAPI:
    """测试手动平仓 API"""
    
    @pytest.fixture
    def client(self):
        """创建测试客户端"""
        with patch('app.v4.api.decision_api.API_KEY', 'test-key'):
            app = create_app(testing=True)
            app.config['TESTING'] = True
            with app.test_client() as client:
                yield client
    
    def test_manual_exit_success(self, client):
        """测试成功手动平仓"""
        mock_result = {
            'code': '0700.HK',
            'action': 'SELL',
            'quantity': 100,
            'price': 401.52,
            'pnl': 1912.00,
            'status': 'executed'
        }
        
        with patch('app.v4.api.decision_api.DecisionEngine') as mock_engine_class:
            mock_engine = Mock()
            mock_engine.manual_exit.return_value = mock_result
            mock_engine_class.get_instance.return_value = mock_engine
            
            response = client.post(
                '/api/v2/manual_exit/0700.HK',
                headers={
                    'X-API-Key': 'test-key',
                    'Content-Type': 'application/json'
                },
                data=json.dumps({'quantity': 100})
            )
            
            assert response.status_code == 200
            data = json.loads(response.data)
            assert data['code'] == '0700.HK'
            assert data['action'] == 'SELL'
            assert data['quantity'] == 100
            assert data['status'] == 'executed'
    
    def test_manual_exit_default_quantity(self, client):
        """测试默认全部平仓"""
        with patch('app.v4.api.decision_api.DecisionEngine') as mock_engine_class:
            mock_engine = Mock()
            mock_engine.manual_exit.return_value = {'code': '0700.HK', 'status': 'executed'}
            mock_engine_class.get_instance.return_value = mock_engine
            
            response = client.post(
                '/api/v2/manual_exit/0700.HK',
                headers={
                    'X-API-Key': 'test-key',
                    'Content-Type': 'application/json'
                },
                data=json.dumps({})
            )
            
            assert response.status_code == 200
            # 验证调用时 quantity 为 None (表示全部)
            mock_engine.manual_exit.assert_called_with('0700.HK', quantity=None)
    
    def test_manual_exit_invalid_quantity(self, client):
        """测试无效数量"""
        response = client.post(
            '/api/v2/manual_exit/0700.HK',
            headers={
                'X-API-Key': 'test-key',
                'Content-Type': 'application/json'
            },
            data=json.dumps({'quantity': -100})
        )
        assert response.status_code == 400


class TestErrorHandling:
    """测试错误处理"""
    
    @pytest.fixture
    def client(self):
        """创建测试客户端"""
        with patch('app.v4.api.decision_api.API_KEY', 'test-key'):
            app = create_app(testing=True)
            app.config['TESTING'] = True
            with app.test_client() as client:
                yield client
    
    def test_engine_not_initialized(self, client):
        """测试引擎未初始化"""
        with patch('app.v4.api.decision_api.DecisionEngine') as mock_engine_class:
            mock_engine_class.get_instance.return_value = None
            
            response = client.get(
                '/api/v2/positions',
                headers={'X-API-Key': 'test-key'}
            )
            
            assert response.status_code == 503
    
    def test_not_found(self, client):
        """测试 404 错误"""
        response = client.get(
            '/api/v2/nonexistent',
            headers={'X-API-Key': 'test-key'}
        )
        assert response.status_code == 404


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
