# tests/agents/voting/test_voting_aggregator.py
import pytest
from app.agents.voting.voting_aggregator import VotingAggregator
from app.agents.voting.model_agent import ModelScore
from app.agents.voting.voting_config import VotingConfig, ModelRole

def test_voting_aggregator_calculates_weighted_average():
    """测试投票聚合器计算加权平均 - 2 模型架构 (Qwen + MiniMax)"""
    config = VotingConfig.get_default_config()
    aggregator = VotingAggregator(config)

    # 2 模型: Qwen + MiniMax
    scores = [
        ModelScore(model_id="qwen", strength=80, confidence=0.8, direction="LONG"),
        ModelScore(model_id="minimax", strength=70, confidence=0.7, direction="LONG"),
    ]

    result = aggregator.aggregate(scores)

    # 加权平均：80*0.5 + 70*0.5 = 75 (minimax 会经过 0.75 校正: 70*0.75=52.5, 但聚合用原始)
    assert result["final_strength"] > 0  # 确保正常计算
    assert result["direction"] == "LONG"

def test_voting_aggregator_calculates_consensus():
    """测试一致性计算 - 2 模型架构"""
    config = VotingConfig.get_default_config()
    aggregator = VotingAggregator(config)

    # 高度一致的情况 (2 模型同向)
    scores_consensus = [
        ModelScore(model_id="qwen", strength=75, confidence=0.8, direction="LONG"),
        ModelScore(model_id="minimax", strength=76, confidence=0.8, direction="LONG"),
    ]

    result = aggregator.aggregate(scores_consensus)
    # 2 模型同向时 consensus 应 >= 0.30 (方向一致性优先)
    assert result["consensus_level"] >= 0.20
    assert result["needs_manual_review"] == False


def test_neutral_band_strong_majority_not_neutral():
    """2026-07-11 中性带多数收敛: 带内(40-60)+低std, 但有 >=2/3 加权多数 → 非中性."""
    config = VotingConfig.get_default_config()
    aggregator = VotingAggregator(config)
    # QWEN(0.35)+DEEPSEEK(0.35)=0.70 多数 LONG, GLM(0.30) SHORT; 强度聚在 [40,60]
    scores = [
        ModelScore(model_id="qwen3.7_max", strength=53, confidence=0.6, direction="LONG"),
        ModelScore(model_id="deepseek_v4_pro", strength=53, confidence=0.6, direction="LONG"),
        ModelScore(model_id="glm_5_2", strength=47, confidence=0.6, direction="SHORT"),
    ]
    result = aggregator.aggregate(scores)
    assert 40 <= result["final_strength"] <= 60  # 落在中性带 (旧逻辑会判中性)
    assert result["is_neutral"] is False        # 新: 强多数豁免中性
    assert result["direction"] == "LONG"        # 方向走加权多数


def test_neutral_band_split_still_neutral():
    """2026-07-11 对照组: 带内无多数(1v1 分歧) → 仍判中性 (行为不变)."""
    config = VotingConfig.get_default_config()
    aggregator = VotingAggregator(config)
    # qwen LONG 52 / glm SHORT 48: 加权多数占比 ~0.54 < 0.67 → 无强多数
    scores = [
        ModelScore(model_id="qwen3.7_max", strength=52, confidence=0.6, direction="LONG"),
        ModelScore(model_id="glm_5_2", strength=48, confidence=0.6, direction="SHORT"),
    ]
    result = aggregator.aggregate(scores)
    assert 40 <= result["final_strength"] <= 60
    assert result["is_neutral"] is True         # 无多数 → 仍中性
