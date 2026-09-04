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
        """True = 此 key 未在 ttl 内见过, 可发送; False = 重复, 跳过.

        DEB-20260903 #110: 仅检查不记录 — 原在返回 True 时立即记录 key,
        发送结果未知; 若实际发送失败, 重试因 key 已存在被误判重复而丢弃.
        记录推迟到发送成功后由 mark_sent() 确认.
        """
        if not key:
            return True
        now = time.time()
        with self._lock:
            ts = self._cache.get(key)
            if ts is not None and (now - ts) < self.ttl:
                return False
        return True

    def mark_sent(self, key: str):
        """发送成功后确认记录 key (DEB-20260903 #110).

        与 should_send 配套: should_send 只 peek, 发送/聚合成功后调 mark_sent
        才真正占据 ttl 窗口; 发送失败不调, 后续重试不会被误判为重复.
        """
        if not key:
            return
        now = time.time()
        with self._lock:
            self._cache[key] = now
            # 简单 LRU: 超过 max_size 时清理过期
            if len(self._cache) > self.max_size:
                cutoff = now - self.ttl
                self._cache = {k: v for k, v in self._cache.items() if v >= cutoff}
