"""
风险管理模块 - FTSeek v3.0

提供五层风控检查、仓位管理、止损策略、实时监控功能

重构于 2026-04-09: 删除冗余文件 (checkers.py, metrics.py, execution.py, dynamic_exit_refactored.py, risk_monitor.py)
"""
from .risk_manager import (
    RiskManager,
    RiskConfig,
    RiskLimit,      # 向后兼容别名
    RiskMetrics,    # 向后兼容
    CheckResult,
    get_risk_manager,
    risk_manager,
)
from .realtime_monitor import init_realtime_monitor

# Phase 3: 波动率监控器
from .vix_monitor import (
    VIXMonitor,
    VolatilityState,
    get_vix_monitor,
)
# Phase 3: 风控状态管理
from .risk_state import (
    RiskStateManager,
    RiskState,
)

__all__ = [
    # 核心风控
    "RiskManager",
    "RiskConfig",
    "RiskLimit",
    "RiskMetrics",
    "CheckResult",
    "get_risk_manager",
    "risk_manager",

    # 实时监控
    "init_realtime_monitor",

    # Phase 3 新增
    "VIXMonitor",
    "VolatilityState",
    "get_vix_monitor",
    "RiskStateManager",
    "RiskState",
]
