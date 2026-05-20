"""
StrategyCoordinator 协调层 (Phase A 2026-05-14)

L3 协调层 — 唯一解决多策略并行冲突的地方:
- CapitalAllocator: 强制各 strategy 资金账本上限
- SymbolLockManager: 同 symbol 同时只许 1 strategy 持仓
- SignalArbitrator: 多策略对同 symbol 出信号 → priority × confidence 仲裁
- ExitDispatcher: 按 position.owner_strategy 路由 ExitPolicy
- StrategyCoordinator: 上述 4 项的唯一编排入口

Phase A 默认 observe_only=True (只记录, 不阻断), 灰度上线后切 enforce.
"""
from .capital_allocator import CapitalAllocator, CapitalSnapshot
from .symbol_lock import SymbolLockManager
from .signal_arbitrator import SignalArbitrator, ArbitrationResult
from .exit_dispatcher import ExitDispatcher
from .strategy_coordinator import StrategyCoordinator, ExecutionResult

__all__ = [
    "CapitalAllocator",
    "CapitalSnapshot",
    "SymbolLockManager",
    "SignalArbitrator",
    "ArbitrationResult",
    "ExitDispatcher",
    "StrategyCoordinator",
    "ExecutionResult",
]
