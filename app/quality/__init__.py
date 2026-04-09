"""
FTSeek 信号质量评估模块 (Signal Quality Framework)

功能:
- 信号质量评分 (Signal Scorer)
- 质量指标定义 (Quality Metrics)
- 历史准确率追踪 (Historical Tracker)
- 市场状态适配 (Regime Adapter)

使用示例:
    from app.quality import SignalQuality, SignalScorer

    scorer = SignalScorer()
    quality = scorer.evaluate(signal, market_regime)

    if quality.score >= 0.65:
        # 高质量信号，推送
        pass
"""

from .signal_scorer import SignalScorer, SignalQuality, QualityLevel
from .quality_metrics import QualityMetrics, QualityThreshold
from .historical_tracker import HistoricalTracker
from .regime_adapter import RegimeAdapter

__all__ = [
    'SignalScorer',
    'SignalQuality',
    'QualityLevel',
    'QualityMetrics',
    'QualityThreshold',
    'HistoricalTracker',
    'RegimeAdapter',
]
