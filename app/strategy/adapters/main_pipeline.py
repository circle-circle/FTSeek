"""MainPipelineAdapter — Phase A 元数据 + T8b regime-aware priority (2026-05-17)

Phase A: 仅声明元数据. 实际信号生成在 app/strategy/pure/technical.py + IntradayScorer.
        coordinator 在 pre_trade 时收到信号后用此 adapter 元数据做仲裁.
T8b: effective_priority 按 regime 浮动 (趋势策略趋势市友好).
"""
from typing import Any, Optional

from app.strategy.base_adapter import StrategyAdapter, AdapterMeta


class MainPipelineAdapter(StrategyAdapter):
    def __init__(self):
        from app.core.config_container import get_config
        cfg = get_config()
        self.meta = AdapterMeta(
            name="main",
            priority=int(cfg.get_dotted("strategies.main.priority", 5)),
            capital_pct=float(cfg.get_dotted("strategies.main.capital_pct", 0.65)),
            data_channels=["1min", "5min", "15min", "L2"],
            market_phases=["regular"],
            description="主线: IntradayScorer 6 因子 (趋势/动量), 趋势市友好",
        )
        super().__init__()

    def effective_priority(self, ctx: Optional[Any] = None) -> int:
        """T8b: 趋势市 +5, 震荡市 -2, 极端市 +5 (main 多层风控更稳)"""
        if ctx is None:
            return self.priority
        regime = ""
        try:
            regime = str(getattr(ctx, "regime", "") or
                         (ctx.get("regime", "") if isinstance(ctx, dict) else "") or
                         "").upper()
        except Exception:
            return self.priority
        if regime in ("TRENDING_UP", "TRENDING_DOWN"):
            return self.priority + 5
        if regime in ("RANGING", "HIGH_VOL_CHOP"):
            return self.priority - 2
        if regime == "EXTREME":
            return self.priority + 5
        return self.priority
