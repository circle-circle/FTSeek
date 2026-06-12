"""
策略模块 — FTSeek-US 重构版

提供：
- BaseStrategy: 增强的策略基类（含 strategy_group, get_parameters, update_parameters, supports_regime）
- StrategyRegistry: 统一策略注册中心
- PerformanceTracker: 按市场状态的策略绩效追踪
- AutoTuner: 策略参数自动调优建议

与 app/decision/strategies/ 的区别：
- 本模块面向调度器层，提供管理接口（注册、绩效追踪、调优）
- decision/strategies/ 保留原有策略算法实现
- 通过 StrategyRegistry 桥接两者
"""
from .base import BaseStrategy, StrategyContext
from .registry import StrategyRegistry, get_strategy_registry

__all__ = [
    "BaseStrategy",
    "StrategyContext",
    "StrategyRegistry",
    "get_strategy_registry",
]
