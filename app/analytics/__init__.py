"""
Analytics 子包 — 业绩归因、基准对照、Alpha 计算

Phase 1 (2026-05-31) 新增:
- BenchmarkTracker: 追踪 SPY/QQQ 日收益率, 计算策略 Alpha
"""

from .benchmark_tracker import BenchmarkTracker, get_benchmark_tracker

__all__ = ["BenchmarkTracker", "get_benchmark_tracker"]
