"""
FTSeek 绩效统计模块
"""
from .stats import (
    PerformanceMetrics,
    PerformanceCalculator,
    PerformanceReporter,
    get_performance_reporter
)
from .tracker import (
    PerformanceTracker,
    get_performance_tracker
)
from .weekly_review import (
    WeeklyReviewer,
    get_weekly_reviewer
)
from .backtest_compare import (
    BacktestComparer,
    get_backtest_comparer
)
from .simulation_metrics import (
    SimulationMetrics,
    SimulationMetricsCalculator,
    get_simulation_metrics_calculator,
    get_simulation_metrics
)

__all__ = [
    # 实盘绩效
    'PerformanceMetrics',
    'PerformanceCalculator',
    'PerformanceReporter',
    'get_performance_reporter',
    'PerformanceTracker',
    'get_performance_tracker',
    'WeeklyReviewer',
    'get_weekly_reviewer',
    'BacktestComparer',
    'get_backtest_comparer',
    # 模拟盘绩效
    'SimulationMetrics',
    'SimulationMetricsCalculator',
    'get_simulation_metrics_calculator',
    'get_simulation_metrics'
]
