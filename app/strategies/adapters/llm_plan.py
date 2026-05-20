"""LLMPlanAdapter — Phase A 元数据 (2026-05-17)

Phase A: 仅元数据. LLM Plan 不直接持仓 (capital_pct=0), 它的输出 (作战计划) 给
        fast_lane / main 提供 watchlist + entry_zone. priority=3 最低.
"""
from app.strategies.base_adapter import StrategyAdapter, AdapterMeta


class LLMPlanAdapter(StrategyAdapter):
    def __init__(self):
        from app.core.config_container import get_config
        cfg = get_config()
        self.meta = AdapterMeta(
            name="llm_plan",
            priority=int(cfg.get_dotted("strategies.llm_plan.priority", 3)),
            capital_pct=float(cfg.get_dotted("strategies.llm_plan.capital_pct", 0.0)),
            data_channels=["daily", "news"],
            market_phases=["premarket", "regular"],
            description="LLM 盘前作战计划 (不直接持仓, 给 main/fast_lane 提供 watchlist + entry_zone)",
        )
        super().__init__()
