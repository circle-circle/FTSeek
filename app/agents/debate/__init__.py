"""
辩论引擎模块 - 多空研究员辩论系统

基于Qwen3.6-Plus(多头)和GLM-5(空头)的多轮辩论机制
"""
from .debate_engine import DebateEngine
from .debate_models import DebateArgument, DebateSummary, DebateRound

__all__ = ["DebateEngine", "DebateArgument", "DebateSummary", "DebateRound"]
