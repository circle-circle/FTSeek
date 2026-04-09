"""
Harness Agent 基类单元测试
"""

import asyncio
import pytest
import time
from datetime import datetime
from typing import Dict, Any

import sys
sys.path.insert(0, '/opt/ftseek/FTSeek')

from app.harness.base import Agent, AgentConfig, AgentState, AgentResult


class TestAgent(Agent):
    """测试用 Agent"""

    async def execute(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """简单返回输入数据"""
        return {"received": input_data, "timestamp": datetime.now().isoformat()}


class SlowAgent(Agent):
    """慢速 Agent (用于超时测试)"""

    async def execute(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """延迟执行"""
        delay = input_data.get("delay", 5)
        await asyncio.sleep(delay)
        return {"completed": True}


class FailingAgent(Agent):
    """失败 Agent (用于重试测试)"""

    def __init__(self, config: AgentConfig, fail_count: int = 3):
        super().__init__(config)
        self.fail_count = fail_count
        self.attempt = 0

    async def execute(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """前 N 次失败，之后成功"""
        self.attempt += 1
        if self.attempt <= self.fail_count:
            raise Exception(f"Intentional failure #{self.attempt}")
        return {"success": True, "attempts": self.attempt}


class TestAgentInitialization:
    """测试 Agent 初始化"""

    def test_agent_creation(self):
        """测试 Agent 创建"""
        config = AgentConfig(name="test-agent")
        agent = TestAgent(config)

        assert agent.name == "test-agent"
        assert agent.state == AgentState.IDLE
        assert agent.is_available is True
        assert agent.is_running is False

    def test_agent_config_defaults(self):
        """测试 Agent 配置默认值"""
        config = AgentConfig(name="test")
        assert config.timeout_seconds == 300
        assert config.retry_count == 2
        assert config.retry_delay == 1.0

    def test_agent_custom_config(self):
        """测试自定义配置"""
        config = AgentConfig(
            name="custom-agent",
            timeout_seconds=60,
            retry_count=3,
            retry_delay=2.0,
        )
        agent = TestAgent(config)

        assert agent.config.timeout_seconds == 60
        assert agent.config.retry_count == 3
        assert agent.config.retry_delay == 2.0


class TestAgentExecution:
    """测试 Agent 执行"""

    @pytest.mark.asyncio
    async def test_agent_execute_success(self):
        """测试成功执行"""
        config = AgentConfig(name="test-agent")
        agent = TestAgent(config)

        result = await agent.run({"key": "value"})

        assert result.success is True
        assert result.data is not None
        assert result.data["received"] == {"key": "value"}
        assert result.error is None
        assert result.execution_time >= 0
        assert result.retry_count == 0

    @pytest.mark.asyncio
    async def test_agent_state_transition(self):
        """测试状态转换"""
        config = AgentConfig(name="test-agent")
        agent = TestAgent(config)

        assert agent.state == AgentState.IDLE

        # 执行中
        async def execute_and_check():
            assert agent.state == AgentState.RUNNING

        # 运行后
        result = await agent.run({"test": "data"})
        assert agent.state == AgentState.COMPLETED
        assert result.success is True

    @pytest.mark.asyncio
    async def test_agent_execution_count(self):
        """测试执行计数"""
        config = AgentConfig(name="test-agent")
        agent = TestAgent(config)

        await agent.run({"test": 1})
        await agent.run({"test": 2})
        await agent.run({"test": 3})

        assert agent._execution_count == 3

        status = agent.get_status()
        assert status["execution_count"] == 3


class TestAgentTimeout:
    """测试 Agent 超时"""

    @pytest.mark.asyncio
    async def test_agent_timeout(self):
        """测试超时处理"""
        config = AgentConfig(
            name="slow-agent",
            timeout_seconds=1,
            retry_count=0,
        )
        agent = SlowAgent(config)

        result = await agent.run({"delay": 5})

        assert result.success is False
        assert result.error is not None
        assert "Timeout" in result.error or "timeout" in result.error.lower()
        assert agent.state == AgentState.TIMEOUT


class TestAgentRetry:
    """测试 Agent 重试"""

    @pytest.mark.asyncio
    async def test_agent_retry_success(self):
        """测试重试后成功"""
        config = AgentConfig(
            name="failing-agent",
            retry_count=3,
            retry_delay=0.1,
        )
        agent = FailingAgent(config, fail_count=2)

        result = await agent.run({"test": "data"})

        assert result.success is True
        assert result.retry_count == 2
        assert result.data is not None
        assert result.data["attempts"] == 3

    @pytest.mark.asyncio
    async def test_agent_retry_exhausted(self):
        """测试重试用尽"""
        config = AgentConfig(
            name="failing-agent",
            retry_count=2,
            retry_delay=0.1,
        )
        agent = FailingAgent(config, fail_count=5)

        result = await agent.run({"test": "data"})

        assert result.success is False
        assert result.retry_count == 2
        assert result.error is not None
        assert agent.state == AgentState.FAILED


class TestAgentCleanup:
    """测试 Agent 清理"""

    @pytest.mark.asyncio
    async def test_agent_context_manager(self):
        """测试异步上下文管理器"""
        config = AgentConfig(name="test-agent")
        agent = TestAgent(config)

        async with agent:
            result = await agent.run({"test": "data"})
            assert result.success is True

        # 上下文退出后应已清理
        assert agent._state == AgentState.COMPLETED

    @pytest.mark.asyncio
    async def test_agent_manual_cleanup(self):
        """测试手动清理"""
        config = AgentConfig(name="test-agent")
        agent = TestAgent(config)

        await agent.initialize()
        await agent.cleanup()

        # 清理后应该仍然可用
        assert agent.is_available is True


class TestAgentStatus:
    """测试 Agent 状态查询"""

    def test_agent_get_status(self):
        """测试获取状态"""
        config = AgentConfig(name="test-agent")
        agent = TestAgent(config)

        status = agent.get_status()

        assert status["name"] == "test-agent"
        assert status["state"] == "idle"
        assert status["execution_count"] == 0
        assert "created_at" in status
        assert status["last_error"] is None

    def test_agent_get_capabilities(self):
        """测试获取能力"""
        config = AgentConfig(
            name="test-agent",
            timeout_seconds=120,
            metadata={"version": "1.0"},
        )
        agent = TestAgent(config)

        caps = agent.get_capabilities()

        assert caps["name"] == "test-agent"
        assert caps["type"] == "TestAgent"
        assert caps["timeout_seconds"] == 120
        assert caps["metadata"]["version"] == "1.0"


class TestAgentResult:
    """测试 AgentResult"""

    def test_result_success(self):
        """测试成功结果"""
        result = AgentResult(
            success=True,
            data={"key": "value"},
            execution_time=1.5,
        )

        assert result.success is True
        assert result.data == {"key": "value"}
        assert result.error is None
        assert result.execution_time == 1.5

    def test_result_failure(self):
        """测试失败结果"""
        result = AgentResult(
            success=False,
            error="Test error",
            execution_time=0.5,
            retry_count=2,
        )

        assert result.success is False
        assert result.error == "Test error"
        assert result.data is None
        assert result.retry_count == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
