"""ScalpingAdapter — Phase A 元数据 + T8b regime-aware priority (2026-05-17)

Phase A: 仅元数据. 实际短线引擎在 app/scalping/engine.py (AUTONOMOUS, 自有 loop).
T8b: effective_priority 按 regime 浮动 (反转策略震荡市友好).
"""
from typing import Any, Optional

from app.strategies.base_adapter import StrategyAdapter, AdapterMeta


class ScalpingAdapter(StrategyAdapter):
    def __init__(self):
        from app.core.config_container import get_config
        cfg = get_config()
        self.meta = AdapterMeta(
            name="scalping",
            priority=int(cfg.get_dotted("strategies.scalping.priority", 10)),
            capital_pct=float(cfg.get_dotted("strategies.scalping.capital_pct", 0.25)),
            data_channels=["1min", "5min", "tick", "news"],
            market_phases=["regular"],
            description="超短线: 反转策略 (RSI/VWAP/BB), 震荡市友好",
        )
        super().__init__()

    def effective_priority(self, ctx: Optional[Any] = None) -> int:
        """T8b: 震荡市 +5, 趋势市 -3, 极端市 -5"""
        if ctx is None:
            return self.priority
        regime = ""
        try:
            regime = str(getattr(ctx, "regime", "") or
                         (ctx.get("regime", "") if isinstance(ctx, dict) else "") or
                         "").upper()
        except Exception:
            return self.priority
        if regime in ("RANGING", "HIGH_VOL_CHOP"):
            return self.priority + 5
        if regime in ("TRENDING_UP", "TRENDING_DOWN"):
            return self.priority - 3
        if regime == "EXTREME":
            return self.priority - 5
        return self.priority
