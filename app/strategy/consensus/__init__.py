"""
共识引擎模块 — 策略共识/权重/LLM 集成/动态权重

目标：将 consensus_engine.py (3150行) 拆分为职责清晰的子模块。

模块边界：
├── engine.py           — 核心投票聚合 (submit/_evaluate)
├── weights.py          — 权重计算 (_calc_weight/_get_hybrid_weight)
├── llm_integration.py  — LLM 集成 (审计/权重自适应)
├── adaptive_weights.py — 动态权重 (AdaptiveWeightManager)
└── audit_log.py        — 审计日志 (信号链/LLM 审计)

使用方式:
    from app.strategy.consensus import ConsensusEngine, WeightCalculator, LLMIntegration
    from app.strategy.consensus import AdaptiveWeightManager, AuditLogger
    
    engine = ConsensusEngine(window_seconds=60, threshold=1.5)
    weights = WeightCalculator()
    llm = LLMIntegration(engine)
"""

from app.strategy.consensus.engine import ConsensusEngine, Vote, ConsensusResult
from app.strategy.consensus.weights import WeightCalculator
from app.strategy.consensus.llm_integration import LLMIntegration
from app.strategy.consensus.adaptive_weights import AdaptiveWeightManager
from app.strategy.consensus.audit_log import AuditLogger, get_audit_logger

# 注意：SignalOutcome 定义在 consensus_engine.py 中
# 如需使用，请从 app.strategy.consensus_engine 导入

__all__ = [
    "ConsensusEngine",
    "Vote",
    "ConsensusResult",
    "WeightCalculator",
    "LLMIntegration",
    "AdaptiveWeightManager",
    "AuditLogger",
    "get_audit_logger",
]
