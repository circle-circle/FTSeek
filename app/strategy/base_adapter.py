"""
StrategyAdapter — Coordinator 接受的最小接口 (Phase A 2026-05-14)

为什么不直接用 app/strategy/base.py 的 BaseStrategy?
- BaseStrategy 是单策略级抽象 (generate_signal)
- StrategyAdapter 是多策略协调级抽象 (priority / capital_pct / data_channels)
- Adapter 内部可以 wrap BaseStrategy 子类, 但要求多了一层

Phase A 各 adapter 仅做元数据声明 + 委托, 不重新实现业务.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, List, Optional


@dataclass
class AdapterMeta:
    """策略元数据 (用于 Coordinator 调度决策)"""
    name: str                      # "main" | "scalping" | "fast_lane" | "llm_plan"
    priority: int                  # 高优先级先决断 (用户决定: scalping=10 > main=5)
    capital_pct: float             # 该策略最大资金占比 (实施 enforce 时强制)
    # B.3 (2026-05-14): 资金桶 — 多策略共享同一资金 bucket
    # 例: fast_lane.capital_bucket="main" → fast_lane 占用计入 main 的 0.65 总额
    # 默认: bucket=name (各自独立账本)
    capital_bucket: Optional[str] = None
    data_channels: List[str] = field(default_factory=lambda: ["1min", "5min"])
    market_phases: List[str] = field(default_factory=lambda: ["regular"])  # premarket / regular / closing
    enabled: bool = True
    description: str = ""

    def __post_init__(self):
        if self.capital_bucket is None:
            self.capital_bucket = self.name


class StrategyAdapter:
    """
    Coordinator 接受的最小策略接口.

    子类必须:
    - 在 __init__ 设置 self.meta = AdapterMeta(...)
    - 实现 generate_signal(symbol, ctx) 返回 SignalScore | None
    - 实现 select_universe(ctx) 返回 [symbol]
    - 实现 get_exit_policy() 返回 ExitPolicy 实例 (Phase B 填充, 现可返回 None)

    内部可以 wrap 现有 BaseStrategy / signal_pipeline / scalping/engine.
    """

    meta: AdapterMeta

    def __init__(self):
        if not hasattr(self, "meta") or self.meta is None:
            raise NotImplementedError("StrategyAdapter 子类必须设置 self.meta")

    @property
    def name(self) -> str:
        return self.meta.name

    @property
    def priority(self) -> int:
        return self.meta.priority

    def effective_priority(self, ctx: Optional[Any] = None) -> int:
        """T8b (2026-05-16): regime-aware 动态 priority.

        子类可 override 此方法, 根据 ctx.regime 调整 priority:
        - RANGING / HIGH_VOL_CHOP → scalping 友好, 反转策略 +5
        - TRENDING_UP / TRENDING_DOWN → main 友好, 趋势策略 +5
        - 默认: 返回静态 self.priority

        SignalArbitrator 调 priority × confidence 时使用此值.
        """
        return self.priority

    @property
    def capital_pct(self) -> float:
        return self.meta.capital_pct

    def select_universe(self, ctx: Any) -> List[str]:
        """选股. Phase A 子类可暂返回空 list — coordinator 会跳过."""
        return []

    def generate_signal(self, symbol: str, ctx: Any) -> Optional[Any]:
        """评估单个 symbol. Phase A 子类可返回 None — coordinator 不会有候选."""
        return None

    def get_exit_policy(self):
        """返回该策略的 ExitPolicy. Phase A 可返回 None — Dispatcher 兜底用 main."""
        return None

    def get_data_channels(self) -> List[str]:
        return list(self.meta.data_channels)

    def is_active_in_phase(self, phase: str) -> bool:
        return phase in self.meta.market_phases or "any" in self.meta.market_phases

    def __repr__(self):
        return f"<{self.__class__.__name__}({self.name}, p={self.priority}, cap={self.capital_pct})>"
