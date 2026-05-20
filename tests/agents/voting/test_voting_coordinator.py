# tests/agents/voting/test_voting_coordinator.py
import pytest
from app.agents.voting.voting_coordinator import VotingCoordinator

def test_voting_coordinator_collects_all_scores():
    """测试投票协调器收集所有模型评分 - P0 优化：2 模型"""
    coordinator = VotingCoordinator()

    market_data = {
        "symbol": "AAPL",
        "price": 150.0,
        "volume_ratio": 1.5,
        "momentum": 0.02,
    }

    result = coordinator.run_voting(market_data)

    assert "final_strength" in result
    assert "model_scores" in result
    assert len(result["model_scores"]) == 2  # P0 优化：2 核心模型 (DeepSeek + MiniMax)

def test_voting_coordinator_handles_timeout():
    """测试超时处理"""
    coordinator = VotingCoordinator(timeout_seconds=1)

    market_data = {"symbol": "AAPL"}

    # 即使有模型超时，也应该返回已有结果
    result = coordinator.run_voting(market_data)
    assert result is not None
