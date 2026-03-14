"""
风险管理模块 - FTSeek v3.0

提供五层风控检查、仓位管理、止损策略、实时监控功能
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

__all__ = [
    "RiskManager",
    "RiskConfig",
    "RiskLimit",      # 向后兼容
    "RiskMetrics",    # 向后兼容
    "CheckResult",
    "get_risk_manager",
    "risk_manager",
    "init_realtime_monitor",
]
