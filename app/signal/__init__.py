#!/usr/bin/env python3
"""
app.signal — 信号子系统统一入口 (P1 2026-05-22)

将分散在多个目录的信号模块通过统一命名空间暴露:
  app.signal.core     — 信号定义、存储、追踪
  app.signal.quality  — 质量评分、融合
  app.signal.analytics — 分析、关联、归因

新代码统一使用:
    from app.signal.core import signal_store, SignalStore
    from app.signal.quality import SignalQualityScorer, SignalScorer
    from app.signal.analytics import SignalAnalytics, get_analytics

旧路径 (app/signal_store.py, app/storage/signal_tracker.py 等) 保持兼容，
存量代码可逐步迁移到 app.signal.* 路径。
"""
