"""3 个策略 adapter (Phase A 元数据 wrap, 2026-05-17 还原)

每个 adapter 仅声明 AdapterMeta + T8b effective_priority(ctx).
实际策略逻辑在:
  - app/strategy/pure/{technical,fast_lane,llm_vote}.py (TICK 类纯函数)
  - app/us_pipeline/premarket_planner.py (AUTONOMOUS, 盘前一次性)
"""
from .main_pipeline import MainPipelineAdapter
from .fast_lane import FastLaneAdapter
from .llm_plan import LLMPlanAdapter

__all__ = [
    "MainPipelineAdapter",
    "FastLaneAdapter",
    "LLMPlanAdapter",
    "all_adapters",
]


def all_adapters():
    """返回 3 个 adapter 实例 (Phase A 协调层用)"""
    return [
        MainPipelineAdapter(),
        FastLaneAdapter(),
        LLMPlanAdapter(),
    ]
