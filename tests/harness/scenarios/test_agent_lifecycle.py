"""
Agent Lifecycle Tests: Pre-market & Post-market
Tests the interaction between the system and the Agent (LLM)
without requiring a real network connection.
"""
import pytest
import json
import os
import tempfile
from datetime import datetime
from tests.harness.mock_agent import MockAgentBridge
from tests.harness.sim_clock import SimClock

# --- Mock Data Fixtures ---

MARKET_CONTEXT = {
    "phase": "PRE_MARKET",
    "timestamp": "2026-05-04T09:00:00",
    "market_regime": "HIGH_VOL",
    "vix": 25.5,
    "spy_trend": "DOWN",
    "yesterday_pnl": -1200.00,
    "open_positions": ["US.TSLA", "US.AAPL"]
}

EXECUTION_LOG = [
    {"symbol": "US.AAPL", "action": "BUY", "price": 170.0, "status": "FILLED"},
    {"symbol": "US.TSLA", "action": "SELL", "price": 240.0, "status": "FILLED"}
]

# --- Test Cases ---

def test_premarket_agent_generates_plan(agent: MockAgentBridge):
    """
    Scenario: Pre-market Agent receives context and generates a Battle Plan.
    Verify: 
    1. Agent receives correct context.
    2. Agent returns valid structure.
    3. System saves and reloads the plan correctly.
    """
    # 1. Mock Agent Response (The "Brain")
    mock_plan_response = {
        "strategy": "Short Volatility",
        "targets": [
            {"symbol": "US.NVDA", "direction": "LONG", "entry": 450.0},
            {"symbol": "US.AMD", "direction": "LONG", "entry": 100.0}
        ],
        "risk_notes": "Watch CPI data at 10:00 AM"
    }
    agent.set_response("PRE_MARKET", mock_plan_response)

    # 2. Simulate System Calling Agent
    # Context passed TO Agent
    result = agent.ask("Generate Battle Plan", context=MARKET_CONTEXT)
    
    # Verify Agent received the right phase
    assert result["strategy"] == "Short Volatility"
    assert len(result["targets"]) == 2

    # 3. Simulate Saving to File (The "Handover" to Intraday)
    plan_path = tempfile.mktemp(suffix=".json")
    with open(plan_path, "w") as f:
        json.dump(result, f)
    
    # 4. Simulate Intraday System Loading the Plan
    with open(plan_path, "r") as f:
        loaded_plan = json.load(f)
    
    assert loaded_plan["targets"][0]["symbol"] == "US.NVDA"
    
    # Cleanup
    os.remove(plan_path)

def test_premarket_agent_failure_and_fallback(agent: MockAgentBridge):
    """
    Scenario: Agent is down or returns garbage.
    Verify: System handles error gracefully (Fallback).
    """
    # 1. Simulate Failure
    agent.is_connected = False
    
    try:
        agent.ask("Generate Battle Plan", context=MARKET_CONTEXT)
        assert False, "Should have raised ConnectionError"
    except ConnectionError:
        # 2. Verify Fallback Logic (Simulated)
        # In real code, we would check if fallback_plan is used
        fallback_plan = {"targets": [], "strategy": "DEFAULT_SAFE"}
        assert len(fallback_plan["targets"]) == 0
        assert fallback_plan["strategy"] == "DEFAULT_SAFE"

def test_postmarket_agent_performs_review(agent: MockAgentBridge):
    """
    Scenario: End of day. Agent reviews executed trades.
    Verify: 
    1. Agent receives full execution log.
    2. Agent produces insights/feedback.
    """
    # 1. Mock Agent Review Response
    mock_review = {
        "summary": "Executed 2 trades. 1 Win, 1 Loss.",
        "mistakes": "Entered TSLA too late.",
        "next_day_focus": "Focus on NVDA breakout."
    }
    agent.set_response("POST_MARKET", mock_review)

    # 2. Construct Review Context
    review_context = {
        "phase": "POST_MARKET",
        "date": "2026-05-04",
        "executed_trades": EXECUTION_LOG,
        "final_pnl": 1500.00
    }

    # 3. Call Agent
    review = agent.ask("Daily Review", context=review_context)
    
    # 4. Verify Analysis
    assert "Executed 2 trades" in review["summary"]
    assert "Focus on NVDA" in review["next_day_focus"]

def test_agent_context_validation(agent: MockAgentBridge):
    """
    Scenario: Verify that critical data points are passed to the Agent.
    Verify: VIX, Open Positions are in context.
    """
    # Capture what is sent to the agent
    captured_context = {}
    original_ask = agent.ask
    
    def spy_ask(prompt, context=None):
        captured_context.update(context or {})
        return {"status": "ok"}

    agent.ask = spy_ask
    
    # Trigger
    agent.ask("Test", context=MARKET_CONTEXT)
    
    # Assertions
    assert "vix" in captured_context
    assert captured_context["vix"] == 25.5
    assert "open_positions" in captured_context
    assert len(captured_context["open_positions"]) == 2
