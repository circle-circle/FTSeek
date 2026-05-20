"""Agent 模块 - 多模型投票系统"""
# P0 清理 2026-04-25: 移除已废弃的 agent 导入
# 仅保留 voting 子系统（实际使用）
from .voting.voting_config import VotingConfig

__all__ = [
    "VotingConfig",
]
