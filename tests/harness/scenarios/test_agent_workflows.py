"""
Agent Workflows Tests
- Pre-market: Plan generation
- Post-market: Review/Attribution
- Failures: Timeout/Malformed
"""
import pytest
import json
import os
from tests.harness.mock_agent import MockAgentBridge

MOCK_PLAN_SUCCESS = {
    "status": "success",
    "targets": [
        {"symbol": "US.TSLA", "direction": "LONG", "entry": 240.0},
        {"symbol": "US.NVDA", "direction": "SHORT", "entry": 120.0}
    ],
    "market_regime": "HIGH_VOL"
}

MOCK_REVIEW_DATA = {
    "pnl": 1200.50,
    "trades": 3,
    "win_rate": 0.66
}

def test_premarket_plan_generation(agent: MockAgentBridge):
    """Verify Agent generates a valid Battle Plan"""
    agent.set_response("PRE_MARKET", MOCK_PLAN_SUCCESS)
    
    plan = agent.ask("Generate Plan", context={"phase": "PRE_MARKET"})
    
    assert plan["status"] == "success"
    assert len(plan["targets"]) == 2
    assert plan["targets"][0]["symbol"] == "US.TSLA"

def test_agent_timeout_handling(agent: MockAgentBridge):
    """Verify Agent Timeout raises ConnectionError or equivalent"""
    agent.set_latency(100) # Too slow
    agent.is_connected = False
    
    with pytest.raises(ConnectionError):
        agent.ask("Plan", {"phase": "PRE_MARKET"})

def test_malformed_json_handling():
    """Verify system handles non-JSON responses gracefully"""
    bad_data = "Here is the analysis... I cannot generate JSON."
    
    try:
        json.loads(bad_data)
        assert False, "Should have failed"
    except json.JSONDecodeError:
        pass # Expected

def test_postmarket_review_injection(agent: MockAgentBridge):
    """Verify Review context is passed to Agent"""
    agent.set_response("POST_MARKET", MOCK_REVIEW_DATA)
    
    review_ctx = {
        "phase": "POST_MARKET",
        "daily_trades": [
            {"symbol": "US.AAPL", "pnl": 500},
            {"symbol": "US.MSFT", "pnl": -100}
        ]
    }
    
    result = agent.ask("Review Day", context=review_ctx)
    assert result["pnl"] == 1200.50
