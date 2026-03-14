"""
因子库模块 - 另类因子实现
"""
from .base import BaseFactor, FactorResult
from .config import FactorConfig, DEFAULT_FACTOR_WEIGHTS
from .capital_flow import CapitalFlowFactors
from .sentiment import SentimentFactors, sentiment_score
from .sentiment_ai import AISentimentFactor
from .market_prediction_ai import AIPredictionFactor
from .esg import ESGFactors
from .preprocessing import FactorPreprocessor

__all__ = [
    'BaseFactor', 'FactorResult', 'FactorConfig',
    'CapitalFlowFactors', 'SentimentFactors', 'AISentimentFactor', 
    'AIPredictionFactor', 'ESGFactors',
    'FactorPreprocessor', 'sentiment_score',
    'DEFAULT_FACTOR_WEIGHTS'
]
