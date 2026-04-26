"""
FTSeek 自愈引擎 (Self-Healing Engine)

模块:
- engine: 自愈引擎核心 + IssueHandler 注册
- optimizer: 自愈优化器，从历史中学习
- dedup: 防重引擎，避免多会话重复分析
"""

from app.self_healing.engine import (
    SelfHealingEngine,
    IssueHandler,
    DiagnosisResult,
    FixResult,
    VerificationResult,
    SelfHealingReport,
    create_engine,
)
from app.self_healing.dedup import (
    DedupStore,
    IssueHasher,
    DedupWrapper,
    create_engine_with_dedup,
)

__all__ = [
    "SelfHealingEngine",
    "IssueHandler",
    "DiagnosisResult",
    "FixResult",
    "VerificationResult",
    "SelfHealingReport",
    "create_engine",
    "DedupStore",
    "IssueHasher",
    "DedupWrapper",
    "create_engine_with_dedup",
]
