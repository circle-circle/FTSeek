"""
决策引擎模块

负责信号生成、评分、市场状态识别、信号去重过滤
"""

from .signal_generator import SignalGenerator
from .signal_scorer import SignalScorer
from .market_regime import MarketRegimeIdentifier

# signal_dedup lives in app.risk, not app.decision (was phantom import)
try:
    from .signal_dedup import SignalDeduplicator
except ImportError:
    from app.risk.signal_dedup import SignalDeduplicator

__all__ = [
    "SignalGenerator",
    "SignalScorer",
    "MarketRegimeIdentifier",
    "SignalDeduplicator",
]
