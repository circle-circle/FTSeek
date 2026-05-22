#!/usr/bin/env python3
"""
app.signal.core — 信号定义、存储与追踪 (P1 SSOT 2026-05-22)

整合:
  - app.signal_store         (内存信号队列 + SQLite 持久化 + 冷却)
  - app.storage.signal_tracker (信号生命周期追踪)

新代码使用:
    from app.signal.core import signal_store, SignalStore
    from app.signal.core import signal_tracker, SignalTracker

旧路径完全兼容，存量代码无需修改。
"""

# ── 内存信号队列 (app/signal_store.py) ──────────────────────────────
from app.signal_store import (
    signal_store,       # 全局单例
    SignalStore,        # 类 (用于测试或新实例)
)

# ── 信号生命周期追踪 (app/storage/signal_tracker.py) ────────────────
from app.storage.signal_tracker import (
    signal_tracker,     # 全局单例
    SignalTracker,      # 类
)

# ── 辅助函数 (如果 signal_store 暴露了 get_signal_tracker) ─────────
try:
    from app.signal_store import get_signal_tracker
except ImportError:
    get_signal_tracker = None

__all__ = [
    'signal_store',
    'SignalStore',
    'signal_tracker',
    'SignalTracker',
    'get_signal_tracker',
]
