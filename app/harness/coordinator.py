"""
Harness 协调器

负责任务分解、Agent 调度和结果汇总。
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from .base import Agent, AgentConfig, AgentResult, AgentState
from .messaging import Message, MessageBus, get_message_bus
from .timeout import TimeoutController, TimeoutConfig

__all__ = ["Coordinator", "TaskDefinition", "WorkflowContext"]


@dataclass
class TaskDefinition:
    """任务定义"""

    name: str  # 任务名称
    agent_name: str  # 执行 Agent 名称
    input_data: Dict[str, Any]  # 输入数据
    timeout_seconds: int = 300  # 超时时间
    dependencies: List[str] = field(default_factory=list)  # 依赖任务
    retry_count: int = 2  # 重试次数
    metadata: Dict[str, Any] = field(default_factory=dict)  # 元数据


@dataclass
class TaskResult:
    """任务执行结果"""

    task_name: str
    success: bool
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    execution_time: float = 0.0
    agent_name: Optional[str] = None


@dataclass
class WorkflowContext:
    """工作流上下文"""

    workflow_id: str
    status: str = "pending"
    created_at: datetime = field(default_factory=datetime.now)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    task_results: Dict[str, TaskResult] = field(default_factory=dict)
    shared_data: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)


class Coordinator:
    """
    协调器

    负责管理多 Agent 协作工作流，包括任务分解、Agent 调度和结果汇总。

    使用示例:
        coordinator = Coordinator()

        # 注册 Agent
        coordinator.register_agent("strategy-gen", strategy_agent)
        coordinator.register_agent("risk-evaluator", risk_agent)

        # 定义工作流
        workflow = [
            TaskDefinition("analyze", "strategy-gen", {"stock": "HK.09992"}),
            TaskDefinition("evaluate", "risk-evaluator", {}, dependencies=["analyze"]),
        ]

        # 执行工作流
        result = await coordinator.execute_workflow("daily_review", workflow)
    """

    def __init__(self, message_bus: Optional[MessageBus] = None):
        """
        初始化协调器

        Args:
            message_bus: 消息总线
        """
        self._agents: Dict[str, Agent] = {}
        self._workflows: Dict[str, WorkflowContext] = {}
        self._message_bus = message_bus or get_message_bus()
        self._logger = logging.getLogger("harness.coordinator")

    def register_agent(self, name: str, agent: Agent) -> None:
        """
        注册 Agent

        Args:
            name: Agent 名称
            agent: Agent 实例
        """
        self._agents[name] = agent
        self._logger.info(f"Agent registered: {name}")

    def unregister_agent(self, name: str) -> None:
        """注销 Agent"""
        if name in self._agents:
            del self._agents[name]
            self._logger.info(f"Agent unregistered: {name}")

    def get_agent(self, name: str) -> Optional[Agent]:
        """获取 Agent"""
        return self._agents.get(name)

    def list_agents(self) -> List[str]:
        """列出所有已注册的 Agent"""
        return list(self._agents.keys())

    async def execute_task(
        self, task: TaskDefinition, context: WorkflowContext
    ) -> TaskResult:
        """
        执行单个任务

        Args:
            task: 任务定义
            context: 工作流上下文

        Returns:
            TaskResult 任务结果
        """
        agent = self._agents.get(task.agent_name)
        if not agent:
            return TaskResult(
                task_name=task.name,
                success=False,
                error=f"Agent not found: {task.agent_name}",
            )

        # 检查依赖
        for dep in task.dependencies:
            if dep not in context.task_results:
                return TaskResult(
                    task_name=task.name,
                    success=False,
                    error=f"Dependency not completed: {dep}",
                )
            dep_result = context.task_results[dep]
            if not dep_result.success:
                return TaskResult(
                    task_name=task.name,
                    success=False,
                    error=f"Dependency failed: {dep}",
                )

        # 准备输入数据
        input_data = task.input_data.copy()
        input_data["_workflow_id"] = context.workflow_id
        input_data["_task_name"] = task.name

        # 添加依赖结果到共享数据
        for dep in task.dependencies:
            dep_result = context.task_results[dep]
            if dep_result.data:
                input_data[f"_{dep}_result"] = dep_result.data

        # 创建超时控制器
        timeout_config = TimeoutConfig(
            timeout_seconds=task.timeout_seconds,
            max_retries=task.retry_count,
        )
        timeout_controller = TimeoutController(timeout_config)

        # 执行任务
        self._logger.info(f"Executing task: {task.name} (agent: {task.agent_name})")

        async def execute_agent():
            return await agent.run(input_data)

        result = await timeout_controller.execute(execute_agent)

        # 转换为 TaskResult
        task_result = TaskResult(
            task_name=task.name,
            success=result.success,
            data=result.data,
            error=result.error,
            execution_time=result.execution_time,
            agent_name=task.agent_name,
        )

        # 存储到上下文
        context.task_results[task.name] = task_result

        # 发布消息
        message = Message.create_event(
            sender="coordinator",
            payload={
                "workflow_id": context.workflow_id,
                "task_name": task.name,
                "status": "completed" if result.success else "failed",
                "execution_time": result.execution_time,
            },
        )
        await self._message_bus.publish(message, channel="workflow")

        return task_result

    async def execute_workflow(
        self,
        workflow_id: str,
        tasks: List[TaskDefinition],
        shared_data: Optional[Dict[str, Any]] = None,
    ) -> WorkflowContext:
        """
        执行工作流

        Args:
            workflow_id: 工作流 ID
            tasks: 任务列表
            shared_data: 共享数据

        Returns:
            WorkflowContext 工作流上下文
        """
        # 创建工作流上下文
        context = WorkflowContext(
            workflow_id=workflow_id,
            status="running",
            shared_data=shared_data or {},
        )
        self._workflows[workflow_id] = context
        context.started_at = datetime.now()

        self._logger.info(f"Starting workflow: {workflow_id} ({len(tasks)} tasks)")

        # 构建任务依赖图
        task_map = {task.name: task for task in tasks}
        completed_tasks = set()
        failed = False

        # 拓扑排序执行
        while len(completed_tasks) < len(tasks):
            # 找出可执行的任务 (依赖已满足)
            ready_tasks = []
            for task in tasks:
                if task.name in completed_tasks:
                    continue
                if all(dep in completed_tasks for dep in task.dependencies):
                    ready_tasks.append(task)

            if not ready_tasks:
                # 没有可执行的任务，但有未完成的任务 -> 循环依赖
                remaining = [t.name for t in tasks if t.name not in completed_tasks]
                context.status = "failed"
                context.completed_at = datetime.now()
                self._logger.error(
                    f"Workflow {workflow_id} failed: circular dependency detected"
                )
                return context

            # 并行执行可执行的任务
            exec_results = await asyncio.gather(
                *[self.execute_task(task, context) for task in ready_tasks],
                return_exceptions=True,
            )

            # 处理结果
            for task, result in zip(ready_tasks, exec_results):
                if isinstance(result, Exception):
                    context.task_results[task.name] = TaskResult(
                        task_name=task.name,
                        success=False,
                        error=str(result),
                    )
                    failed = True
                elif not result.success:
                    failed = True

                completed_tasks.add(task.name)
                self._logger.info(
                    f"Task {task.name} completed: "
                    f"{'success' if result.success else 'failed'}"
                )

        # 工作流完成
        context.status = "completed" if not failed else "failed"
        context.completed_at = datetime.now()

        total_time = (context.completed_at - context.started_at).total_seconds()
        self._logger.info(
            f"Workflow {workflow_id} {context.status} in {total_time:.2f}s"
        )

        return context

    async def execute_parallel(
        self,
        workflow_id: str,
        tasks: List[TaskDefinition],
    ) -> WorkflowContext:
        """
        并行执行任务 (无依赖)

        Args:
            workflow_id: 工作流 ID
            tasks: 任务列表

        Returns:
            WorkflowContext 工作流上下文
        """
        # 清除所有依赖
        for task in tasks:
            task.dependencies = []

        return await self.execute_workflow(workflow_id, tasks)

    def get_workflow_status(self, workflow_id: str) -> Optional[Dict[str, Any]]:
        """
        获取工作流状态

        Args:
            workflow_id: 工作流 ID

        Returns:
            状态字典
        """
        context = self._workflows.get(workflow_id)
        if not context:
            return None

        return {
            "workflow_id": workflow_id,
            "status": context.status,
            "created_at": context.created_at.isoformat(),
            "started_at": context.started_at.isoformat() if context.started_at else None,
            "completed_at": context.completed_at.isoformat() if context.completed_at else None,
            "task_count": len(context.task_results),
            "success_count": sum(1 for r in context.task_results.values() if r.success),
            "failed_count": sum(1 for r in context.task_results.values() if not r.success),
        }

    def list_workflows(self) -> List[str]:
        """列出所有工作流"""
        return list(self._workflows.keys())
