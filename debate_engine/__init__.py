"""
独立辩论引擎 (Independent Debate Engine)

不绑定任何市场，不限定轮次，不限定触发方式。
给定议题 → 多模型充分对抗 → 结构化结论 → 可注入因子/退出模块。

核心组件:
- DebateTopic: 议题定义
- DynamicRoundScheduler: 动态轮次调度 (收敛驱动)
- DebateKnowledgeBase: 知识库 (历史教训持久化)
- DebateVerdict: 结构化结论 + 注入通道
- OutcomeTracker: 结论回填追踪
"""
from debate_engine.topic import DebateTopic, TopicType
from debate_engine.verdict import DebateVerdict, Injection, InjectionApplier
from debate_engine.knowledge import DebateKnowledgeBase, Lesson, DebateRecord, Outcome

__all__ = [
    "DebateTopic", "TopicType",
    "DebateVerdict", "Injection", "InjectionApplier",
    "DebateKnowledgeBase", "Lesson", "DebateRecord", "Outcome",
]
