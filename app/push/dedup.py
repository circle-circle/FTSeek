"""1min 窗口同 key 去重 (2026-05-16)

防止同一事件 (例: 同 trace_id 的开仓重复触发) 短时间内多次推送.
设计上比 AlertDebouncer 更轻量 (不做升级, 只去重).
"""
from __future__ import annotations
import threading
import time
from typing import Dict


class DedupCache:
    """简单 LRU 去重缓存 (线程安全)"""

    def __init__(self, ttl_sec: int = 60, max_size: int = 1000):
        self.ttl = ttl_sec
        self.max_size = max_size
        self._cache: Dict[str, float] = {}
        self._lock = threading.Lock()

    def should_send(self, key: str) -> bool:
        """True = 此 key 未在 ttl 内见过, 可发送; False = 重复, 跳过."""
        if not key:
            return True
        now = time.time()
        with self._lock:
            ts = self._cache.get(key)
            if ts is not None and (now - ts) < self.ttl:
                return False
            self._cache[key] = now
            # 简单 LRU: 超过 max_size 时清理过期
            if len(self._cache) > self.max_size:
                cutoff = now - self.ttl
                self._cache = {k: v for k, v in self._cache.items() if v >= cutoff}
        return True
