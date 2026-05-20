import pytest
from app.agents.agent_config import AgentRole, AgentConfig

def test_agent_role_definition():
    """测试 Agent 角色定义"""
    assert AgentRole.MAIN.value == "main"
    assert AgentRole.DECISION.value == "decision"
    assert AgentRole.RISK.value == "risk"
    assert AgentRole.EXECUTION.value == "execution"
    assert AgentRole.OBSERVABILITY.value == "observability"

def test_agent_config_initialization():
    """测试 Agent 配置初始化"""
    config = AgentConfig.get_config(AgentRole.MAIN)
    
    assert config.role == AgentRole.MAIN
    assert config.model != ""
    assert config.description != ""
