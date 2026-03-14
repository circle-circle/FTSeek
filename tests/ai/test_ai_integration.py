"""
AI 模块完整测试
"""
import pytest
import sys
from pathlib import Path
import os

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

if not os.getenv('BAILIAN_API_KEY'):
    os.environ['BAILIAN_API_KEY'] = 'test_key'

from app.ai.base import AIResponse
from app.ai.cost_tracker import CostTracker, get_cost_tracker
from app.ai.cache import AICache, get_ai_cache, cached
from app.ai.config import AIConfig, is_ai_enabled, get_ai_provider, get_ai_weight


class TestCostTracker:
    """测试成本追踪器"""
    
    def test_init(self):
        tracker = CostTracker()
        assert tracker.daily_budget == 100.0
        assert tracker.monthly_budget == 3000.0
    
    def test_record_call(self):
        tracker = CostTracker()
        initial_calls = tracker.stats.daily_calls
        
        tracker.record_call(tokens_used=500, cost=0.005)
        
        assert tracker.stats.daily_calls == initial_calls + 1
        assert tracker.stats.daily_cost == 0.005
    
    def test_can_call(self):
        tracker = CostTracker()
        assert tracker.can_call() == True
    
    def test_get_status(self):
        tracker = CostTracker()
        status = tracker.get_status()
        
        assert 'daily' in status
        assert 'monthly' in status
        assert 'remaining' in status['daily']
    
    def test_singleton(self):
        tracker1 = get_cost_tracker()
        tracker2 = get_cost_tracker()
        assert tracker1 is tracker2


class TestAICache:
    """测试 AI 缓存"""
    
    def test_init(self):
        cache = AICache(ttl=3600, max_size=1000)
        assert cache.ttl == 3600
        assert cache.max_size == 1000
    
    def test_set_get(self):
        cache = AICache()
        cache.set('test_key', {'data': 'test'})
        
        result = cache.get('test_key')
        assert result == {'data': 'test'}
    
    def test_cache_miss(self):
        cache = AICache()
        result = cache.get('nonexistent_key')
        assert result is None
    
    def test_clear(self):
        cache = AICache()
        cache.set('key1', 'value1')
        cache.set('key2', 'value2')
        
        cache.clear()
        
        assert cache.get('key1') is None
        assert cache.get('key2') is None
    
    def test_get_stats(self):
        cache = AICache()
        cache.set('key1', 'value1')
        cache.set('key2', 'value2')
        
        stats = cache.get_stats()
        
        assert stats['total_entries'] == 2
        assert stats['valid_entries'] == 2
        assert stats['max_size'] == 1000
    
    def test_singleton(self):
        cache1 = get_ai_cache()
        cache2 = get_ai_cache()
        assert cache1 is cache2


class TestCachedDecorator:
    """测试缓存装饰器"""
    
    def test_cached_function(self):
        call_count = {'count': 0}
        
        @cached(ttl=3600)
        def expensive_function(x, y):
            call_count['count'] += 1
            return x + y
        
        # 第一次调用
        result1 = expensive_function(1, 2)
        assert result1 == 3
        assert call_count['count'] == 1
        
        # 第二次调用 (应该命中缓存)
        result2 = expensive_function(1, 2)
        assert result2 == 3
        assert call_count['count'] == 1  # 不应该增加


class TestAIConfig:
    """测试 AI 配置"""
    
    def test_from_env_default(self, monkeypatch):
        monkeypatch.setenv('FTSEEK_ENABLE_AI', 'false')
        monkeypatch.setenv('AI_PROVIDER', 'bailian')
        monkeypatch.setenv('FTSEEK_AI_WEIGHT', '0.3')
        
        config = AIConfig.from_env()
        
        assert config.enabled == False
        assert config.provider == 'bailian'
        assert config.weight == 0.3
    
    def test_from_env_custom(self, monkeypatch):
        monkeypatch.setenv('FTSEEK_ENABLE_AI', 'true')
        monkeypatch.setenv('AI_PROVIDER', 'qwen')
        monkeypatch.setenv('FTSEEK_AI_WEIGHT', '0.5')
        monkeypatch.setenv('AI_DAILY_BUDGET', '200')
        
        config = AIConfig.from_env()
        
        assert config.enabled == True
        assert config.provider == 'qwen'
        assert config.weight == 0.5
        assert config.daily_budget == 200.0
    
    def test_to_dict(self):
        config = AIConfig(enabled=True, provider='bailian', weight=0.3)
        config_dict = config.to_dict()
        
        assert config_dict['enabled'] == True
        assert config_dict['provider'] == 'bailian'
        assert config_dict['weight'] == 0.3


class TestConfigHelpers:
    """测试配置辅助函数"""
    
    def test_is_ai_enabled(self, monkeypatch):
        monkeypatch.setenv('FTSEEK_ENABLE_AI', 'false')
        assert is_ai_enabled() == False
        
        monkeypatch.setenv('FTSEEK_ENABLE_AI', 'true')
        assert is_ai_enabled() == True
    
    def test_get_ai_provider(self, monkeypatch):
        monkeypatch.setenv('AI_PROVIDER', 'qwen')
        assert get_ai_provider() == 'qwen'
    
    def test_get_ai_weight(self, monkeypatch):
        monkeypatch.setenv('FTSEEK_AI_WEIGHT', '0.5')
        assert get_ai_weight() == 0.5
