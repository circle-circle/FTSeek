# tests/agents/test_coordinator.py
import pytest
from app.agents.coordinator import AgentCoordinator

def test_coordinator_initialization():
    """测试协调器初始化"""
    coordinator = AgentCoordinator()
    
    assert coordinator is not None
    assert hasattr(coordinator, 'dispatch_to_agent')

def test_coordinator_dispatch_workflow():
    """测试协调器分发工作流"""
    coordinator = AgentCoordinator()
    
    # 模拟工作流：decision → risk → execution → observability
    workflow = ["decision", "risk", "execution", "observability"]
    
    # 验证工作流定义
    assert workflow == coordinator.get_default_workflow()
