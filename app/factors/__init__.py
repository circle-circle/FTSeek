"""
因子库模块 - 另类因子实现

新增因子库，与现有 app/strategy/ 完全独立，不影响现有功能。
"""
from .base import BaseFactor, FactorResult
from .config import FactorConfig, DEFAULT_FACTOR_WEIGHTS
from .capital_flow import CapitalFlowFactors
from .sentiment import SentimentFactors, sentiment_score
from .esg import ESGFactors
from .preprocessing import FactorPreprocessor

__all__ = [
    'BaseFactor', 'FactorResult', 'FactorConfig',
    'CapitalFlowFactors', 'SentimentFactors', 'ESGFactors',
    'FactorPreprocessor', 'sentiment_score',
    'DEFAULT_FACTOR_WEIGHTS'
]
