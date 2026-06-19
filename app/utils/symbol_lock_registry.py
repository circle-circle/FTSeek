"""
Per-symbol lock registry (P1-4 2026-05-14)

替代 scheduler._open_position_lock 的全局 RLock — 慢策略持锁时, 其他信号被全部阻塞.

行为:
- 每个 symbol 一把 RLock (按需创建)
- 同一 symbol 不同 strategy 仍然互斥 (业务正确性)
- 不同 symbol 完全并行
- timeout 支持: acquire(symbol, timeout=5.0) 超时返回 False
- 反向兼容: 提供 acquire/release 上下文管理器

使用:
    from app.utils.symbol_lock_registry import get_symbol_lock_registry
    reg = get_symbol_lock_registry()
    with reg.lock("NVDA", timeout=5.0) as got:
        if not got:
            return  # 没拿到锁
        ... # 临界区
"""
from __future__ import annotations
import threading
import time
from contextlib import contextmanager
from typing import Dict, Optional


class SymbolLockRegistry:
    def __init__(self):
        self._locks: Dict[str, threading.RLock] = {}
        self._meta_lock = threading.Lock()
        # GC 不活跃锁 — 防止内存泄漏 (无锁的 symbol 长期不再交易)
        self._last_used: Dict[str, float] = {}
        self._gc_threshold_sec = 3600  # 1 小时未用 GC

    def _get_or_create(self, symbol: str) -> threading.RLock:
        with self._meta_lock:
            if symbol not in self._locks:
                self._locks[symbol] = threading.RLock()
            self._last_used[symbol] = time.time()
            return self._locks[symbol]

    @contextmanager
    def lock(self, symbol: str, timeout: float = -1):
        """
        上下文管理器. yield True 拿到锁, False 超时.

        timeout=-1: 无限等待 (RLock 默认)
        timeout=0:  立即试一次, 拿不到返回 False
        timeout=N:  等 N 秒
        """
        lk = self._get_or_create(symbol)
        if timeout < 0:
            lk.acquire()
            try:
                yield True
            finally:
                lk.release()
            return
        # 带超时
        got = lk.acquire(timeout=timeout) if timeout > 0 else lk.acquire(blocking=False)
        try:
            yield got
        finally:
            if got:
                lk.release()

    def gc_idle_locks(self):
        """清理长时间未用的锁 — 单例长期运行时调"""
        with self._meta_lock:
            cutoff = time.time() - self._gc_threshold_sec
            to_remove = [s for s, ts in self._last_used.items() if ts < cutoff]
            # 仅清理无人持有的锁 (用 acquire(blocking=False) 测)
            for s in to_remove:
                lk = self._locks.get(s)
                if lk and lk.acquire(blocking=False):
                    try:
                        del self._locks[s]
                        del self._last_used[s]
                    finally:
                        lk.release()


_singleton: Optional[SymbolLockRegistry] = None


def get_symbol_lock_registry() -> SymbolLockRegistry:
    global _singleton
    if _singleton is None:
        _singleton = SymbolLockRegistry()
    return _singleton


def acquire_symbol_lock(symbol: str, timeout: float = -1):
    """简化接口 — 返回 ContextManager"""
    return get_symbol_lock_registry().lock(symbol, timeout=timeout)
