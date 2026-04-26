"""共识辅助模块

2026-04-26 收敛：vote_collector / score_calculator / decision_maker 三个子模块无任何外部调用方，已删除。
真正的共识引擎实现在 app/strategy/consensus_engine.py。
本包仅保留：
- WeightCalculator / DEFAULT_WEIGHTS：供 attribution 归因引擎与 scripts/daily_attribution_report 使用
- event_performance_tracker：供 cross_market、backtest、examples 使用
"""
from .weight_calculator import WeightCalculator, DEFAULT_WEIGHTS

__all__ = ["WeightCalculator", "DEFAULT_WEIGHTS"]
