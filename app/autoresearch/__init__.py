"""
AutoResearch - 策略参数自动研究

基于 AutoResearch 项目原理，实现 FTSeek 策略参数自动优化。
"""

from .search_engine import AutoResearch, ParameterSpace, SearchResult
from .overfitting_detector import OverfittingDetector
from .backtest_integration import AutoResearchBacktest
from .strategies_config import STRATEGY_PARAM_SPACES, OPTIMIZATION_TARGETS, get_param_space
from .optimizer import StrategyOptimizer
from .backtest_adapter import BacktestAdapter, BacktestResult
from .deployment import StrategyDeployment
from .monitor import StrategyMonitor

__version__ = "1.0.0"
__all__ = [
    "AutoResearch",
    "ParameterSpace",
    "SearchResult",
    "OverfittingDetector",
    "AutoResearchBacktest",
    "StrategyOptimizer",
    "BacktestAdapter",
    "BacktestResult",
    "StrategyDeployment",
    "StrategyMonitor",
    "STRATEGY_PARAM_SPACES",
    "OPTIMIZATION_TARGETS",
    "get_param_space",
]
