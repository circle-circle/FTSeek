"""
TTL缓存管理器 (P0 修复 2026-04-11)

解决内存泄漏问题:
- 为所有字典缓存添加TTL清理机制
- 防止长期运行导致的OOM
- 自动清理过期和不再使用的条目

使用场景:
- K线缓存
- 交易锁
- 冷却字典
- 价格缓存
"""

import time
import threading
from collections import OrderedDict
from typing import Any, Optional, Callable, Dict
import logging

logger = logging.getLogger("ftseek.utils.ttl_cache")


class TTLCache:
    """
    带TTL的线程安全缓存
    
    特性:
    - 自动过期清理
    - LRU淘汰策略
    - 线程安全
    - 可配置TTL和最大大小
    """
    
    def __init__(self, ttl: int = 3600, maxsize: int = 1000, cleanup_interval: int = 300):
        """
        初始化TTL缓存
        
        Args:
            ttl: 默认过期时间 (秒)
            maxsize: 最大缓存条目数
            cleanup_interval: 自动清理间隔 (秒)
        """
        self.ttl = ttl
        self.maxsize = maxsize
        self.cleanup_interval = cleanup_interval
        
        self._cache: OrderedDict = OrderedDict()  # key -> (value, timestamp)
        self._lock = threading.RLock()
        self._closed = False
        
        # 启动后台清理线程
        self._cleanup_thread = threading.Thread(
            target=self._background_cleanup,
            daemon=True,
            name="TTLCacheCleanup"
        )
        self._cleanup_thread.start()
        
        logger.info(f"TTL缓存已初始化 | TTL={ttl}s | 最大大小={maxsize} | 清理间隔={cleanup_interval}s")
    
    def get(self, key: str, default: Any = None) -> Any:
        """
        获取缓存值
        
        Args:
            key: 缓存键
            default: 默认值 (如果不存在或已过期)
        
        Returns:
            缓存值或默认值
        """
        with self._lock:
            if key not in self._cache:
                return default
            
            value, timestamp = self._cache[key]
            
            # 检查是否过期
            if time.time() - timestamp > self.ttl:
                del self._cache[key]
                return default
            
            # LRU: 移动到末尾
            self._cache.move_to_end(key)
            return value
    
    def set(self, key: str, value: Any, ttl: int = None):
        """
        设置缓存值
        
        Args:
            key: 缓存键
            value: 缓存值
            ttl: 自定义TTL (秒)，默认使用初始化值
        """
        ttl = ttl if ttl is not None else self.ttl
        
        with self._lock:
            # 如果key已存在，先删除
            if key in self._cache:
                del self._cache[key]
            
            # 如果缓存已满，删除最旧的
            if len(self._cache) >= self.maxsize:
                oldest_key, _ = self._cache.popitem(last=False)
                logger.debug(f"缓存已满，淘汰最旧条目: {oldest_key}")
            
            # 插入新值
            self._cache[key] = (value, time.time())
    
    def delete(self, key: str) -> bool:
        """
        删除缓存条目
        
        Args:
            key: 缓存键
        
        Returns:
            是否删除成功
        """
        with self._lock:
            if key in self._cache:
                del self._cache[key]
                return True
            return False
    
    def exists(self, key: str) -> bool:
        """
        检查键是否存在且未过期
        
        Args:
            key: 缓存键
        
        Returns:
            是否存在
        """
        with self._lock:
            if key not in self._cache:
                return False
            
            _, timestamp = self._cache[key]
            if time.time() - timestamp > self.ttl:
                del self._cache[key]
                return False
            
            return True
    
    def cleanup_expired(self) -> int:
        """
        清理所有过期条目
        
        Returns:
            清理的条目数
        """
        with self._lock:
            now = time.time()
            expired_keys = [
                key for key, (_, timestamp) in self._cache.items()
                if now - timestamp > self.ttl
            ]
            
            for key in expired_keys:
                del self._cache[key]
            
            if expired_keys:
                logger.debug(f"清理了 {len(expired_keys)} 个过期条目")
            
            return len(expired_keys)
    
    def _background_cleanup(self):
        """后台清理线程"""
        while not self._closed:
            try:
                time.sleep(self.cleanup_interval)
                cleaned = self.cleanup_expired()
                if cleaned > 0:
                    logger.info(f"后台清理完成 | 清理条目={cleaned} | 当前大小={len(self._cache)}")
            except Exception as e:
                logger.error(f"后台清理失败: {e}")
    
    def clear(self):
        """清空所有缓存"""
        with self._lock:
            self._cache.clear()
            logger.info("缓存已清空")
    
    def close(self):
        """关闭缓存，停止后台清理线程"""
        self._closed = True
        logger.info("缓存已关闭")
    
    @property
    def size(self) -> int:
        """当前缓存条目数"""
        return len(self._cache)
    
    def get_stats(self) -> Dict[str, Any]:
        """获取缓存统计信息"""
        with self._lock:
            now = time.time()
            expired_count = sum(
                1 for _, (_, ts) in self._cache.items()
                if now - ts > self.ttl
            )
            
            return {
                'total_entries': len(self._cache),
                'expired_entries': expired_count,
                'maxsize': self.maxsize,
                'ttl': self.ttl,
                'utilization': len(self._cache) / self.maxsize if self.maxsize > 0 else 0
            }


class LockCache:
    """
    线程锁缓存 (专门用于管理动态创建的锁)
    
    解决 _trade_locks 只增不减的问题
    """
    
    def __init__(self, ttl: int = 600, maxsize: int = 500):
        """
        初始化锁缓存
        
        Args:
            ttl: 锁的过期时间 (秒)，默认10分钟
            maxsize: 最大锁数量
        """
        self.ttl = ttl
        self.maxsize = maxsize
        
        self._locks: Dict[str, tuple] = {}  # key -> (lock, timestamp)
        self._lock = threading.RLock()
        
        # 启动后台清理
        self._cleanup_thread = threading.Thread(
            target=self._background_cleanup,
            daemon=True,
            name="LockCacheCleanup"
        )
        self._cleanup_thread.start()
        
        logger.info(f"锁缓存已初始化 | TTL={ttl}s | 最大数量={maxsize}")
    
    def get_lock(self, key: str) -> threading.Lock:
        """
        获取或创建锁
        
        Args:
            key: 锁的键
        
        Returns:
            threading.Lock
        """
        with self._lock:
            now = time.time()
            
            # 如果锁存在且未过期，更新时间戳并返回
            if key in self._locks:
                lock, timestamp = self._locks[key]
                if now - timestamp <= self.ttl:
                    self._locks[key] = (lock, now)  # 更新时间戳
                    return lock
                else:
                    # 已过期，删除旧锁
                    del self._locks[key]
            
            # 如果缓存已满，清理过期锁
            if len(self._locks) >= self.maxsize:
                self._cleanup_expired_internal()
            
            # 创建新锁
            new_lock = threading.Lock()
            self._locks[key] = (new_lock, now)
            return new_lock
    
    def _cleanup_expired_internal(self):
        """内部清理过期锁 (必须在持有 self._lock 时调用)"""
        now = time.time()
        expired_keys = [
            key for key, (_, timestamp) in self._locks.items()
            if now - timestamp > self.ttl
        ]
        
        for key in expired_keys:
            del self._locks[key]
        
        if expired_keys:
            logger.debug(f"清理了 {len(expired_keys)} 个过期锁")
    
    def _background_cleanup(self):
        """后台清理线程"""
        while True:
            try:
                time.sleep(self.ttl / 2)  # 每半个TTL周期清理一次
                with self._lock:
                    self._cleanup_expired_internal()
                    logger.debug(f"锁缓存后台清理 | 当前数量={len(self._locks)}")
            except Exception as e:
                logger.error(f"锁缓存后台清理失败: {e}")
    
    def remove(self, key: str):
        """手动删除锁"""
        with self._lock:
            if key in self._locks:
                del self._locks[key]
    
    @property
    def size(self) -> int:
        """当前锁数量"""
        return len(self._locks)
