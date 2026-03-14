"""
AI 缓存层

减少 API 调用，降低成本
"""
import time
import hashlib
import logging
from typing import Optional, Dict, Any
from functools import wraps

logger = logging.getLogger("ftseek.ai.cache")


class AICache:
    """AI 响应缓存"""
    
    def __init__(self, ttl: int = 3600, max_size: int = 1000):
        """
        初始化缓存
        
        Args:
            ttl: 缓存生存时间 (秒)，默认 1 小时
            max_size: 最大缓存条目数
        """
        self.ttl = ttl
        self.max_size = max_size
        self._cache: Dict[str, tuple] = {}
        self._access_order = []
    
    def _generate_key(self, *args, **kwargs) -> str:
        """生成缓存键"""
        key_data = str(args) + str(sorted(kwargs.items()))
        return hashlib.md5(key_data.encode()).hexdigest()
    
    def get(self, key: str) -> Optional[Any]:
        """获取缓存"""
        if key not in self._cache:
            return None
        
        data, timestamp = self._cache[key]
        
        if time.time() - timestamp > self.ttl:
            # 缓存过期
            del self._cache[key]
            if key in self._access_order:
                self._access_order.remove(key)
            return None
        
        # 更新访问顺序
        if key in self._access_order:
            self._access_order.remove(key)
        self._access_order.append(key)
        
        return data
    
    def set(self, key: str, data: Any):
        """设置缓存"""
        # 检查大小限制
        if len(self._cache) >= self.max_size:
            # 移除最旧的条目
            if self._access_order:
                oldest_key = self._access_order[0]
                del self._cache[oldest_key]
                self._access_order.pop(0)
                logger.debug(f"缓存已满，移除最旧条目：{oldest_key[:8]}...")
        
        self._cache[key] = (data, time.time())
        
        if key in self._access_order:
            self._access_order.remove(key)
        self._access_order.append(key)
        
        logger.debug(f"缓存已设置：{key[:8]}... (TTL={self.ttl}s)")
    
    def clear(self):
        """清空缓存"""
        self._cache.clear()
        self._access_order.clear()
        logger.info("缓存已清空")
    
    def get_stats(self) -> Dict:
        """获取缓存统计"""
        now = time.time()
        valid_count = sum(
            1 for _, ts in self._cache.values()
            if now - ts <= self.ttl
        )
        
        return {
            'total_entries': len(self._cache),
            'valid_entries': valid_count,
            'expired_entries': len(self._cache) - valid_count,
            'max_size': self.max_size,
            'ttl': self.ttl,
            'memory_usage_estimate': f"{len(self._cache) * 1024 / 1024:.2f} MB"
        }
    
    def cleanup_expired(self) -> int:
        """清理过期缓存"""
        now = time.time()
        expired_keys = [
            key for key, (_, ts) in self._cache.items()
            if now - ts > self.ttl
        ]
        
        for key in expired_keys:
            del self._cache[key]
            if key in self._access_order:
                self._access_order.remove(key)
        
        if expired_keys:
            logger.info(f"清理 {len(expired_keys)} 条过期缓存")
        
        return len(expired_keys)


# 装饰器
def cached(ttl: int = 3600):
    """
    缓存装饰器
    
    用法:
        @cached(ttl=3600)
        def analyze_sentiment(text: str) -> AIResponse:
            ...
    """
    cache = AICache(ttl=ttl)
    
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            # 生成缓存键
            key = cache._generate_key(*args, **kwargs)
            
            # 尝试获取缓存
            cached_result = cache.get(key)
            if cached_result is not None:
                logger.debug(f"缓存命中：{func.__name__}")
                return cached_result
            
            # 调用函数
            result = func(*args, **kwargs)
            
            # 设置缓存
            cache.set(key, result)
            
            return result
        
        wrapper.cache = cache
        return wrapper
    
    return decorator


# 全局缓存实例
_ai_cache = None

def get_ai_cache(ttl: int = 3600) -> AICache:
    """获取 AI 缓存单例"""
    global _ai_cache
    if _ai_cache is None:
        _ai_cache = AICache(ttl=ttl)
    return _ai_cache
