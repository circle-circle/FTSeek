import pytest
from app.agents.risk_agent import RiskAgent
from app.agents.execution_agent import ExecutionAgent
from app.agents.observability_agent import ObservabilityAgent

def test_risk_agent_pre_trade_check():
    """测试风险 Agent 预交易检查"""
    agent = RiskAgent()
    
    signal = {
        "strength": 75,
        "confidence": 0.8,
    }
    
    result = agent.pre_trade_check(signal)
    assert "approved" in result

def test_execution_agent_submit_order():
    """测试执行 Agent 提交订单"""
    agent = ExecutionAgent()
    
    order = {
        "symbol": "AAPL",
        "quantity": 100,
        "side": "BUY",
    }
    
    result = agent.submit_order(order)
    assert "order_id" in result

def test_observability_agent_record_event():
    """测试观测 Agent 记录事件"""
    agent = ObservabilityAgent()
    
    event = {
        "event_type": "order_submitted",
        "symbol": "AAPL",
    }
    
    result = agent.record_event(event)
    assert result["success"] == True
