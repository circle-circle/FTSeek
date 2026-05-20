import pytest
from app.agents.voting.model_agent import ModelAgent, ModelScore

def test_model_agent_generates_score():
    """测试模型 Agent 生成评分"""
    agent = ModelAgent("deepseek", "deepseek/deepseek-chat")
    
    market_data = {
        "symbol": "AAPL",
        "price": 150.0,
        "volume_ratio": 1.5,
        "momentum": 0.02,
    }
    
    score = agent.generate_score(market_data)
    
    assert score is not None
    assert 0 <= score.strength <= 100
    assert 0.0 <= score.confidence <= 1.0
    assert score.model_id == "deepseek"

def test_model_score_serialization():
    """测试模型评分序列化"""
    score = ModelScore(
        model_id="deepseek",
        strength=75,
        confidence=0.8,
        reasoning="趋势强劲，成交量放大"
    )
    
    data = score.to_dict()
    assert "model_id" in data
    assert "strength" in data
