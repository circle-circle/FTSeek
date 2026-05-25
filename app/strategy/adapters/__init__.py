"""4 个策略 adapter (Phase A 元数据 wrap, 2026-05-17 还原)

每个 adapter 仅声明 AdapterMeta + T8b effective_priority(ctx).
实际策略逻辑在:
  - app/strategy/pure/{technical,fast_lane,llm_vote}.py (TICK 类纯函数)
  - app/scalping/engine.py (AUTONOMOUS, 自有 loop)
  - app/us_pipeline/premarket_planner.py (AUTONOMOUS, 盘前一次性)

历史背景: 2026-05-16 曾尝试新建 app/strategies/contract+registry+base_strategy
重写为 Strategy ABC. 经终验发现与现有 app/strategy/pure/ 重复造轮子, 已回退.
详见 docs/PREDEPLOY_AUDIT_2026-05-17.md.
"""
from .main_pipeline import MainPipelineAdapter
from .scalping import ScalpingAdapter
from .fast_lane import FastLaneAdapter
from .llm_plan import LLMPlanAdapter

__all__ = [
    "MainPipelineAdapter",
    "ScalpingAdapter",
    "FastLaneAdapter",
    "LLMPlanAdapter",
    "all_adapters",
]


def all_adapters():
    """返回 4 个 adapter 实例 (Phase A 协调层用)"""
    return [
        MainPipelineAdapter(),
        ScalpingAdapter(),
        FastLaneAdapter(),
        LLMPlanAdapter(),
    ]
