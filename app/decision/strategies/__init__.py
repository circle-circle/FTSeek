"""
策略模块 - 多策略支持

P1 模块化重构 (2026-04-06):
- 策略独立成模块，便于快速更换/优化/补充
- 统一策略基类和接口
- 自动注册机制
"""

from .base import BaseStrategy, StrategyResult, register_strategy, get_strategy, list_strategies
from .price_break import PriceBreakStrategy
from .ma_cross import MACrossStrategy
from .rsi_rebound import RSIReboundStrategy

__all__ = [
    # 基类
    "BaseStrategy",
    "StrategyResult",
    # 注册函数
    "register_strategy",
    "get_strategy",
    "list_strategies",
    # 内置策略
    "PriceBreakStrategy",
    "MACrossStrategy",
    "RSIReboundStrategy",
]
