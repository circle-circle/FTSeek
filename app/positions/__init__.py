"""持仓生命周期 + 多源集成 (2026-05-16)

跨 broker/manual/strategy 统一管理头寸, 默认 PROTECTED 隔离.

公开 API:
    PositionSource, ProtectionState
    get_position_source(pos), get_protection_state(pos), tag_position(...)
    is_strategy_managed(pos), is_protected(pos)
    StartupReconciler / run_startup_reconcile(store, broker)
    ProtectionController.takeover/release/freeze
    RiskAggregator.snapshot(equity)

铁律:
    1. 不修改现有信号生成与核心交易逻辑
    2. 所有外部头寸默认 PROTECTED, 显式 takeover 才 MANAGED
"""
from .source import (
    PositionSource, ProtectionState,
    get_position_source, get_protection_state, tag_position,
    is_strategy_managed, is_protected,
)
from .protection import ProtectionController, get_protection_controller
from .reconciler import StartupReconciler, ReconcileResult, run_startup_reconcile
from .risk_aggregator import RiskAggregator, RiskExposureSnapshot

__all__ = [
    "PositionSource", "ProtectionState",
    "get_position_source", "get_protection_state", "tag_position",
    "is_strategy_managed", "is_protected",
    "ProtectionController", "get_protection_controller",
    "StartupReconciler", "ReconcileResult", "run_startup_reconcile",
    "RiskAggregator", "RiskExposureSnapshot",
]
