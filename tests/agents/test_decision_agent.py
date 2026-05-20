# tests/agents/test_decision_agent.py
import pytest
from app.agents.decision_agent import DecisionAgent

def test_decision_agent_generates_signal():
    """测试决策 Agent 生成信号"""
    agent = DecisionAgent()
    
    market_data = {
        "symbol": "AAPL",
        "price": 150.0,
        "volume_ratio": 1.5,
    }
    
    signal = agent.generate_signal(market_data)
    
    assert signal is not None
    assert "signal_id" in signal
    assert "strength" in signal
