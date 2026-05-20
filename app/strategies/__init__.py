"""
Strategy Adapters (Phase A 2026-05-14)

把现有 4 条策略包成统一接口, 供 StrategyCoordinator 调度.
不重写业务逻辑 — 各 adapter 仅做参数映射 + 委托.

注意: 与 app/strategy/ (单数) 区别 ——
  - app/strategy/    : 已有 BaseStrategy + Registry 框架
  - app/strategies/  : 本目录, 装 Adapter (符合 Coordinator 接口)

Phase A 仅建空壳 + name/priority/capital_pct 元数据;
Phase B 在 generate_signal / get_exit_policy 内填充具体逻辑.
"""
from .base_adapter import StrategyAdapter, AdapterMeta

__all__ = ["StrategyAdapter", "AdapterMeta"]
