#!/usr/bin/env python3
"""
app.signal.analytics — 信号分析、关联与归因 (P1 SSOT 2026-05-22)

整合:
  - app.analytics.signal_analytics    (信号结果追踪与策略/场景分析)
  - app.analytics.signal_analyzer     (信号详情记录与多维因子分析)
  - app.analytics.signal_correlation  (信号质量相关性分析)

新代码使用:
    from app.signal.analytics import SignalAnalytics, get_analytics
    from app.signal.analytics import record_signal_detail
    from app.signal.analytics import get_signal_quality_correlation

旧路径完全兼容，存量代码无需修改。
"""

# ── 信号结果分析 (app/analytics/signal_analytics.py) ───────────────
from app.analytics.signal_analytics import (
    SignalAnalytics,
    get_analytics,
    init_analytics,
)

# ── 信号详情记录 (app/analytics/signal_analyzer.py) ────────────────
from app.analytics.signal_analyzer import record_signal_detail

# ── 信号相关性分析 (app/analytics/signal_correlation.py) ───────────
from app.analytics.signal_correlation import get_signal_quality_correlation

__all__ = [
    'SignalAnalytics',
    'get_analytics',
    'init_analytics',
    'record_signal_detail',
    'get_signal_quality_correlation',
]
