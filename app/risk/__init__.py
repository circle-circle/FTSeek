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
from .checkers import (
    RiskCheckEngine,
    PositionChecker,
    TradeLimitChecker,
    DrawdownChecker,
    ConsecutiveLossChecker,
    ConfidenceChecker,
    create_check_engine,
)
from .metrics import (
    RiskConfig as NewRiskConfig,
    RiskLimit as NewRiskLimit,
    RiskMetrics as NewRiskMetrics,
    CheckResult as NewCheckResult,
    RiskCheckResult,
    StopLossType,
    create_risk_config,
    create_risk_metrics,
    create_risk_limits,
)
from .execution import (
    StopLossExecutor,
    TakeProfitExecutor,
    BreakevenExecutor,
    PositionSizer,
    EmergencyLiquidator,
    create_stop_loss_executor,
    create_take_profit_executor,
    create_breakeven_executor,
    create_position_sizer,
    create_emergency_liquidator,
)
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
    # 原有导出 (向后兼容)
    "RiskManager",
    "RiskConfig",
    "RiskLimit",
    "RiskMetrics",
    "CheckResult",
    "get_risk_manager",
    "risk_manager",
    "init_realtime_monitor",
    # 新模块导出
    "RiskCheckEngine",
    "PositionChecker",
    "TradeLimitChecker",
    "DrawdownChecker",
    "ConsecutiveLossChecker",
    "ConfidenceChecker",
    "create_check_engine",
    "NewRiskConfig",
    "NewRiskLimit",
    "NewRiskMetrics",
    "NewCheckResult",
    "RiskCheckResult",
    "StopLossType",
    "create_risk_config",
    "create_risk_metrics",
    "create_risk_limits",
    "StopLossExecutor",
    "TakeProfitExecutor",
    "BreakevenExecutor",
    "PositionSizer",
    "EmergencyLiquidator",
    "create_stop_loss_executor",
    "create_take_profit_executor",
    "create_breakeven_executor",
    "create_position_sizer",
    "create_emergency_liquidator",
    # Phase 3 新增
    "VIXMonitor",
    "VolatilityState",
    "get_vix_monitor",
    "RiskStateManager",
    "RiskState",
]
