"""
AI 服务测试
"""
import pytest
import sys
from pathlib import Path
import os

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

if not os.getenv('BAILIAN_API_KEY'):
    os.environ['BAILIAN_API_KEY'] = 'test_key'

from app.ai.base import AIService, AIResponse
from app.ai.providers.bailian import BailianProvider


class TestAIResponse:
    """测试 AIResponse"""
    
    def test_success_response(self):
        response = AIResponse.success_response(
            data={'text': 'test'},
            tokens=100,
            cost=0.01,
            provider='bailian'
        )
        assert response.success == True
        assert response.data['text'] == 'test'
        assert response.tokens_used == 100
    
    def test_error_response(self):
        response = AIResponse.error_response(
            error='API error',
            provider='bailian'
        )
        assert response.success == False
        assert response.error == 'API error'


class TestBailianProvider:
    """测试 Bailian Provider"""
    
    def test_init(self):
        provider = BailianProvider()
        assert provider.name == 'bailian'
        assert provider.model == 'qwen-plus'
    
    def test_parse_prediction(self):
        provider = BailianProvider()
        text = """方向：上涨
置信度：75
理由：技术面突破"""
        
        result = provider._parse_prediction(text)
        assert result['direction'] == '上涨'
        assert result['confidence'] == 75
        assert '突破' in result['reason']
    
    def test_cost_calculation(self):
        provider = BailianProvider()
        cost = provider._calculate_cost(1000)
        assert cost == 0.01


class TestAIService:
    """测试 AIService"""
    
    def test_init_default(self):
        service = AIService()
        assert service.provider.name == 'bailian'
    
    def test_init_qwen(self):
        service = AIService(provider='qwen')
        # 应该回退到 bailian (如果 qwen 未实现)
        assert service.provider.name in ['qwen', 'bailian']
