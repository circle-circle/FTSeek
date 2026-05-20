"""
决策引擎模块

负责信号生成、评分、市场状态识别、信号去重过滤
"""

from .signal_generator import SignalGenerator
from .signal_scorer import SignalScorer
from .market_regime import MarketRegimeIdentifier
from .signal_dedup import SignalDeduplicator
from .consensus import ConsensusEngine

__all__ = [
    "SignalGenerator",
    "SignalScorer",
    "MarketRegimeIdentifier",
    "SignalDeduplicator",
    "ConsensusEngine",
]
