"""
Harness Agent 基类

提供所有 Agent 的抽象基类，定义统一接口和行为规范。
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

__all__ = ["AgentState", "AgentConfig", "AgentResult", "Agent"]


class AgentState(Enum):
    """Agent 运行状态"""

    IDLE = "idle"  # 空闲
    RUNNING = "running"  # 运行中
    COMPLETED = "completed"  # 完成
    FAILED = "failed"  # 失败
    TIMEOUT = "timeout"  # 超时
    RETRYING = "retrying"  # 重试中


@dataclass
class AgentConfig:
    """Agent 配置"""

    name: str  # Agent 名称
    timeout_seconds: int = 300  # 超时时间 (秒)
    retry_count: int = 2  # 重试次数
    retry_delay: float = 1.0  # 重试延迟 (秒)
    log_level: int = logging.INFO  # 日志级别
    metadata: Dict[str, Any] = field(default_factory=dict)  # 元数据


@dataclass
class AgentResult:
    """Agent 执行结果"""

    success: bool  # 是否成功
    data: Optional[Dict[str, Any]] = None  # 返回数据
    error: Optional[str] = None  # 错误信息
    execution_time: float = 0.0  # 执行时间 (秒)
    retry_count: int = 0  # 实际重试次数
    metadata: Dict[str, Any] = field(default_factory=dict)  # 元数据


class Agent(ABC):
    """
    Agent 抽象基类

    所有 Harness Agent 必须继承此类并实现抽象方法。

    使用示例:
        class MyAgent(Agent):
            async def execute(self, input_data: Dict) -> Dict:
                # 实现具体逻辑
                return {"result": "success"}

        agent = MyAgent(config)
        result = await agent.run({"key": "value"})
    """

    def __init__(self, config: AgentConfig):
        """
        初始化 Agent

        Args:
            config: Agent 配置
        """
        self.config = config
        self.name = config.name
        self._state = AgentState.IDLE
        self._last_error: Optional[str] = None
        self._execution_count = 0
        self._created_at = datetime.now()
        self._started_at: Optional[datetime] = None
        self._completed_at: Optional[datetime] = None

        # 初始化日志器
        self.logger = logging.getLogger(f"harness.{self.name}")
        self.logger.setLevel(config.log_level)

        if not self.logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter(
                "%(asctime)s | %(process)d | %(thread)d | "
                "[%(name)s:%(filename)s:%(lineno)d] %(levelname)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)

    @property
    def state(self) -> AgentState:
        """获取当前状态"""
        return self._state

    @property
    def is_running(self) -> bool:
        """检查是否正在运行"""
        return self._state == AgentState.RUNNING

    @property
    def is_available(self) -> bool:
        """检查是否可用"""
        return self._state in (AgentState.IDLE, AgentState.COMPLETED)

    @abstractmethod
    async def execute(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行 Agent 任务 (子类必须实现)

        Args:
            input_data: 输入数据

        Returns:
            执行结果字典

        Raises:
            Exception: 执行失败时抛出异常
        """
        pass

    def get_capabilities(self) -> Dict[str, Any]:
        """
        获取 Agent 能力描述

        Returns:
            能力描述字典
        """
        return {
            "name": self.name,
            "type": self.__class__.__name__,
            "timeout_seconds": self.config.timeout_seconds,
            "retry_count": self.config.retry_count,
            "metadata": self.config.metadata,
        }

    def get_status(self) -> Dict[str, Any]:
        """
        获取 Agent 状态

        Returns:
            状态字典
        """
        return {
            "name": self.name,
            "state": self._state.value,
            "execution_count": self._execution_count,
            "last_error": self._last_error,
            "created_at": self._created_at.isoformat(),
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "completed_at": self._completed_at.isoformat() if self._completed_at else None,
        }

    async def initialize(self) -> None:
        """
        初始化 Agent (可选重载)

        子类可重载此方法进行初始化操作。
        """
        self.logger.info(f"Agent {self.name} initialized")

    async def cleanup(self) -> None:
        """
        清理 Agent 资源 (可选重载)

        子类可重载此方法进行资源清理。
        """
        self.logger.info(f"Agent {self.name} cleanup completed")

    async def _execute_with_timeout(
        self, input_data: Dict[str, Any], timeout: float
    ) -> Dict[str, Any]:
        """
        带超时的执行包装器

        Args:
            input_data: 输入数据
            timeout: 超时时间 (秒)

        Returns:
            执行结果

        Raises:
            asyncio.TimeoutError: 超时时抛出
        """
        return await asyncio.wait_for(self.execute(input_data), timeout=timeout)

    async def run(self, input_data: Dict[str, Any]) -> AgentResult:
        """
        运行 Agent (带重试和超时控制)

        Args:
            input_data: 输入数据

        Returns:
            AgentResult 执行结果
        """
        self._execution_count += 1
        self._started_at = datetime.now()
        self._state = AgentState.RUNNING

        start_time = datetime.now()
        last_error: Optional[str] = None

        for attempt in range(self.config.retry_count + 1):
            try:
                self.logger.info(
                    f"Agent {self.name} executing (attempt {attempt + 1}/"
                    f"{self.config.retry_count + 1})"
                )

                # 执行带超时的任务
                result_data = await self._execute_with_timeout(
                    input_data, self.config.timeout_seconds
                )

                # 执行成功
                self._state = AgentState.COMPLETED
                self._completed_at = datetime.now()
                execution_time = (self._completed_at - start_time).total_seconds()

                self.logger.info(
                    f"Agent {self.name} completed in {execution_time:.2f}s"
                )

                return AgentResult(
                    success=True,
                    data=result_data,
                    execution_time=execution_time,
                    retry_count=attempt,
                )

            except asyncio.TimeoutError as e:
                last_error = f"Timeout after {self.config.timeout_seconds}s"
                self.logger.warning(
                    f"Agent {self.name} timeout (attempt {attempt + 1})"
                )
                self._state = AgentState.TIMEOUT

            except Exception as e:
                last_error = str(e)
                self.logger.error(f"Agent {self.name} error: {e}", exc_info=True)
                self._state = AgentState.FAILED

            # 重试逻辑
            if attempt < self.config.retry_count:
                self._state = AgentState.RETRYING
                self.logger.info(
                    f"Agent {self.name} retrying in {self.config.retry_delay}s..."
                )
                await asyncio.sleep(self.config.retry_delay)

        # 所有重试失败
        self._completed_at = datetime.now()
        execution_time = (self._completed_at - start_time).total_seconds()
        self._last_error = last_error

        self.logger.error(
            f"Agent {self.name} failed after {self.config.retry_count + 1} attempts: "
            f"{last_error}"
        )

        return AgentResult(
            success=False,
            error=last_error,
            execution_time=execution_time,
            retry_count=self.config.retry_count,
        )

    async def __aenter__(self) -> "Agent":
        """异步上下文管理器入口"""
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """异步上下文管理器出口"""
        await self.cleanup()

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={self.name}, state={self._state.value})"
