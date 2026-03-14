"""
因子库模块 - 另类因子实现

新增因子库，与现有 app/strategy/ 完全独立，不影响现有功能。
"""
from .base import BaseFactor, FactorResult
from .config import FactorConfig

__all__ = ['BaseFactor', 'FactorResult', 'FactorConfig']
