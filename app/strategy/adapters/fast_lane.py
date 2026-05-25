"""FastLaneAdapter — Phase A 元数据 (2026-05-17)

Phase A: 仅元数据. fast_lane 实际逻辑在 app/strategy/pure/fast_lane.py.
        capital_bucket=main → fast_lane 占用计入 main 桶 (不双计).
"""
from app.strategy.base_adapter import StrategyAdapter, AdapterMeta


class FastLaneAdapter(StrategyAdapter):
    def __init__(self):
        from app.core.config_container import get_config
        cfg = get_config()
        self.meta = AdapterMeta(
            name="fast_lane",
            priority=int(cfg.get_dotted("strategies.fast_lane.priority", 8)),
            capital_pct=float(cfg.get_dotted("strategies.fast_lane.capital_pct", 0.20)),
            capital_bucket="main",  # B.3 (2026-05-14): 共享 main 桶, 不双计
            data_channels=["5min", "L2"],
            market_phases=["regular"],
            description="快车道: battle_plan entry_zone 匹配, 资金计入 main bucket",
        )
        super().__init__()
