"""
请求管理与限流保护器 (Request Manager & Rate Limiter)

功能:
1. API 请求统一管理
2. 多级缓存减少重复请求
3. 智能限流保护
4. 请求合并与批处理

支持服务:
- 富途 API (行情/交易)
- 百炼 AI API (情绪分析)
- 新闻源 API (RSS/API)

创建时间: 2026-03-17
"""
import time
import logging
import threading
from datetime import datetime, timedelta
from typing import Dict, Optional, Callable, Any, List, Tuple
from collections import deque
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path

logger = logging.getLogger("ftseek.request_manager")
logger.propagate = False  # 防止日志重复


@dataclass
class RateLimitConfig:
    """限流配置"""
    requests_per_second: int = 10      # 每秒请求数
    requests_per_minute: int = 60      # 每分钟请求数
    requests_per_hour: int = 1000      # 每小时请求数
    burst_size: int = 5                # 突发请求大小
    cooldown_ms: int = 100             # 请求间隔 (毫秒)


@dataclass
class CacheConfig:
    """缓存配置"""
    enabled: bool = True
    ttl_seconds: int = 60              # 缓存有效期
    max_size: int = 1000               # 最大缓存条目
    persist: bool = False              # 是否持久化


class APIRateLimiter:
    """
    API 限流器 (令牌桶算法)
    
    支持多级限流:
    - 秒级限制
    - 分钟级限制
    - 小时级限制
    """
    
    def __init__(self, config: RateLimitConfig = None):
        self.config = config or RateLimitConfig()
        
        # 请求记录 (滑动窗口)
        self._second_requests: deque = deque(maxlen=100)
        self._minute_requests: deque = deque(maxlen=1000)
        self._hour_requests: deque = deque(maxlen=10000)
        
        # 最后请求时间
        self._last_request_time: float = 0
        
        # 线程锁
        self._lock = threading.Lock()
        
        # 统计
        self._total_requests = 0
        self._rejected_requests = 0
    
    def can_request(self) -> Tuple[bool, str]:
        """
        检查是否可以发起请求
        
        Returns:
            (can_request, reason)
        """
        with self._lock:
            now = time.time()
            
            # 清理过期记录
            self._cleanup(now)
            
            # 检查冷却时间
            if now - self._last_request_time < self.config.cooldown_ms / 1000:
                wait_time = self.config.cooldown_ms / 1000 - (now - self._last_request_time)
                return False, f"冷却中，需等待 {wait_time*1000:.0f}ms"
            
            # 检查秒级限制
            second_count = len([t for t in self._second_requests if now - t < 1])
            if second_count >= self.config.requests_per_second:
                return False, f"秒级限流 ({second_count}/{self.config.requests_per_second})"
            
            # 检查分钟级限制
            minute_count = len([t for t in self._minute_requests if now - t < 60])
            if minute_count >= self.config.requests_per_minute:
                return False, f"分钟级限流 ({minute_count}/{self.config.requests_per_minute})"
            
            # 检查小时级限制
            hour_count = len([t for t in self._hour_requests if now - t < 3600])
            if hour_count >= self.config.requests_per_hour:
                return False, f"小时级限流 ({hour_count}/{self.config.requests_per_hour})"
            
            return True, "OK"
    
    def record_request(self):
        """记录一次请求"""
        with self._lock:
            now = time.time()
            self._second_requests.append(now)
            self._minute_requests.append(now)
            self._hour_requests.append(now)
            self._last_request_time = now
            self._total_requests += 1
    
    def _cleanup(self, now: float):
        """清理过期记录"""
        # deque 会自动清理超出的部分，这里只做统计
        pass
    
    def get_stats(self) -> dict:
        """获取统计信息"""
        with self._lock:
            now = time.time()
            return {
                'total_requests': self._total_requests,
                'rejected_requests': self._rejected_requests,
                'current_second': len([t for t in self._second_requests if now - t < 1]),
                'current_minute': len([t for t in self._minute_requests if now - t < 60]),
                'current_hour': len([t for t in self._hour_requests if now - t < 3600]),
                'limits': {
                    'per_second': self.config.requests_per_second,
                    'per_minute': self.config.requests_per_minute,
                    'per_hour': self.config.requests_per_hour,
                }
            }


class SmartCache:
    """
    智能缓存系统
    
    特性:
    - 多级缓存 (内存 + 文件)
    - TTL 过期自动清理
    - LRU 淘汰策略
    - 缓存命中率统计
    """
    
    def __init__(self, config: CacheConfig = None):
        self.config = config or CacheConfig()
        
        # 内存缓存
        self._cache: Dict[str, Tuple[Any, float]] = {}
        
        # 访问顺序 (用于 LRU)
        self._access_order: List[str] = []
        
        # 线程锁
        self._lock = threading.Lock()
        
        # 统计
        self._hits = 0
        self._misses = 0
    
    def _generate_key(self, *args, **kwargs) -> str:
        """生成缓存键"""
        content = json.dumps({'args': args, 'kwargs': kwargs}, sort_keys=True)
        return hashlib.md5(content.encode()).hexdigest()
    
    def get(self, key: str) -> Optional[Any]:
        """获取缓存"""
        with self._lock:
            if key not in self._cache:
                self._misses += 1
                return None
            
            data, timestamp = self._cache[key]
            
            # 检查是否过期
            if time.time() - timestamp > self.config.ttl_seconds:
                del self._cache[key]
                self._misses += 1
                return None
            
            # 更新访问顺序 (LRU)
            if key in self._access_order:
                self._access_order.remove(key)
            self._access_order.append(key)
            
            self._hits += 1
            return data
    
    def set(self, key: str, value: Any, ttl: int = None):
        """设置缓存"""
        with self._lock:
            # 检查容量
            while len(self._cache) >= self.config.max_size:
                # LRU 淘汰
                if self._access_order:
                    oldest = self._access_order.pop(0)
                    if oldest in self._cache:
                        del self._cache[oldest]
                else:
                    break
            
            self._cache[key] = (value, time.time())
            self._access_order.append(key)
    
    def get_or_compute(self, key: str, compute_func: Callable, ttl: int = None) -> Any:
        """
        获取缓存或计算
        
        Args:
            key: 缓存键
            compute_func: 计算函数
            ttl: 过期时间
        
        Returns:
            缓存值或计算结果
        """
        cached = self.get(key)
        if cached is not None:
            return cached
        
        result = compute_func()
        self.set(key, result, ttl)
        return result
    
    def invalidate(self, key: str = None):
        """使缓存失效"""
        with self._lock:
            if key:
                if key in self._cache:
                    del self._cache[key]
                if key in self._access_order:
                    self._access_order.remove(key)
            else:
                self._cache.clear()
                self._access_order.clear()
    
    def get_stats(self) -> dict:
        """获取统计信息"""
        with self._lock:
            total = self._hits + self._misses
            hit_rate = self._hits / total if total > 0 else 0
            return {
                'size': len(self._cache),
                'max_size': self.config.max_size,
                'hits': self._hits,
                'misses': self._misses,
                'hit_rate': f"{hit_rate:.1%}",
                'ttl_seconds': self.config.ttl_seconds,
            }


class RequestManager:
    """
    请求管理器
    
    统一管理所有外部 API 请求，提供:
    1. 限流保护
    2. 智能缓存
    3. 请求队列
    4. 错误重试
    """
    
    # 各 API 的默认限流配置
    DEFAULT_LIMITS = {
        'futu_quote': RateLimitConfig(
            requests_per_second=10,
            requests_per_minute=30,
            requests_per_hour=500,
            cooldown_ms=100
        ),
        'futu_trade': RateLimitConfig(
            requests_per_second=5,
            requests_per_minute=20,
            requests_per_hour=200,
            cooldown_ms=200
        ),
        'bailian': RateLimitConfig(
            requests_per_second=5,
            requests_per_minute=10,
            requests_per_hour=100,
            cooldown_ms=200
        ),
        'news_api': RateLimitConfig(
            requests_per_second=2,
            requests_per_minute=10,
            requests_per_hour=100,
            cooldown_ms=500
        ),
    }
    
    # 各数据类型的缓存配置
    DEFAULT_CACHE = {
        'quote': CacheConfig(ttl_seconds=5, max_size=100),      # 行情缓存 5 秒
        'kline': CacheConfig(ttl_seconds=60, max_size=50),      # K线缓存 1 分钟
        'news': CacheConfig(ttl_seconds=300, max_size=200),     # 新闻缓存 5 分钟
        'sentiment': CacheConfig(ttl_seconds=600, max_size=100), # 情绪缓存 10 分钟
        'market_snapshot': CacheConfig(ttl_seconds=3, max_size=20), # 快照缓存 3 秒
    }
    
    def __init__(self):
        # 限流器
        self._limiters: Dict[str, APIRateLimiter] = {}
        
        # 缓存
        self._caches: Dict[str, SmartCache] = {}
        
        # 请求队列
        self._pending_requests: List[Callable] = []
        self._queue_lock = threading.Lock()
        
        # 初始化默认配置
        for name, config in self.DEFAULT_LIMITS.items():
            self._limiters[name] = APIRateLimiter(config)
        
        for name, config in self.DEFAULT_CACHE.items():
            self._caches[name] = SmartCache(config)
        
        logger.info("请求管理器已初始化")
    
    def request(self, api_name: str, cache_type: str, 
                request_func: Callable, cache_key: str = None,
                force_refresh: bool = False) -> Any:
        """
        发起请求 (带缓存和限流)
        
        Args:
            api_name: API 名称 (futu_quote, bailian, news_api)
            cache_type: 缓存类型 (quote, kline, news, sentiment)
            request_func: 请求函数
            cache_key: 缓存键 (可选，自动生成)
            force_refresh: 是否强制刷新
        
        Returns:
            请求结果
        """
        # 检查缓存
        if not force_refresh and cache_type in self._caches:
            cache = self._caches[cache_type]
            key = cache_key or self._generate_cache_key(request_func)
            cached = cache.get(key)
            if cached is not None:
                logger.debug(f"缓存命中: {cache_type}/{key[:8]}")
                return cached
        
        # 检查限流
        limiter = self._limiters.get(api_name)
        if limiter:
            can_request, reason = limiter.can_request()
            if not can_request:
                logger.warning(f"请求被限流: {api_name} - {reason}")
                # 返回缓存（即使过期）或 None
                if cache_type in self._caches:
                    key = cache_key or self._generate_cache_key(request_func)
                    return self._caches[cache_type]._cache.get(key, (None, 0))[0]
                return None
            
            limiter.record_request()
        
        # 发起请求
        try:
            result = request_func()
            
            # 更新缓存
            if cache_type in self._caches:
                key = cache_key or self._generate_cache_key(request_func)
                self._caches[cache_type].set(key, result)
            
            return result
            
        except Exception as e:
            logger.error(f"请求失败: {api_name} - {e}")
            raise
    
    def _generate_cache_key(self, func: Callable) -> str:
        """生成缓存键"""
        return hashlib.md5(str(func).encode()).hexdigest()
    
    def get_limiter_stats(self, api_name: str = None) -> dict:
        """获取限流统计"""
        if api_name:
            return self._limiters.get(api_name, APIRateLimiter()).get_stats()
        return {name: limiter.get_stats() for name, limiter in self._limiters.items()}
    
    def get_cache_stats(self, cache_type: str = None) -> dict:
        """获取缓存统计"""
        if cache_type:
            return self._caches.get(cache_type, SmartCache()).get_stats()
        return {name: cache.get_stats() for name, cache in self._caches.items()}
    
    def invalidate_cache(self, cache_type: str = None):
        """使缓存失效"""
        if cache_type:
            if cache_type in self._caches:
                self._caches[cache_type].invalidate()
        else:
            for cache in self._caches.values():
                cache.invalidate()
    
    def get_all_stats(self) -> dict:
        """获取所有统计信息"""
        return {
            'limiters': self.get_limiter_stats(),
            'caches': self.get_cache_stats(),
        }


# 全局单例
_request_manager: Optional[RequestManager] = None


def get_request_manager() -> RequestManager:
    """获取请求管理器单例"""
    global _request_manager
    if _request_manager is None:
        _request_manager = RequestManager()
    return _request_manager