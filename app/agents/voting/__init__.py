"""投票模块 — 2026-07-14: 死投票脑(coordinator/aggregator/model_agent)已清理,
仅保留 VotingConfig/ModelRole 供 app/risk/vix_monitor.py 读 VIX 权重常量。"""
from .voting_config import VotingConfig, ModelRole

__all__ = [
    "VotingConfig",
    "ModelRole",
]
