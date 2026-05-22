#!/usr/bin/env python3
"""
app.signal.quality — 信号质量评分与融合 (P1 SSOT 2026-05-22)

整合:
  - app.strategy.signal_quality_scorer (共识引擎使用的质量评分器)
  - app.quality.signal_scorer          (多维度质量评分: QualityLevel)
  - app.strategy.signal_coordinator    (跨市场信号协调)

新代码使用:
    from app.signal.quality import get_default_scorer
    from app.signal.quality import SignalScorer, SignalQuality, QualityLevel
    from app.signal.quality import SignalCoordinator

旧路径完全兼容，存量代码无需修改。
"""

# ── 共识引擎质量评分器 (app/strategy/signal_quality_scorer.py) ─────
from app.strategy.signal_quality_scorer import (
    get_default_scorer,  # 获取默认评分器单例
)

# ── 多维度质量评分 (app/quality/signal_scorer.py) ──────────────────
from app.quality.signal_scorer import (
    SignalScorer,
    SignalQuality,
    QualityLevel,
)

# ── 信号协调 (app/strategy/signal_coordinator.py) ──────────────────
from app.strategy.signal_coordinator import SignalCoordinator

__all__ = [
    'get_default_scorer',
    'SignalScorer',
    'SignalQuality',
    'QualityLevel',
    'SignalCoordinator',
]
