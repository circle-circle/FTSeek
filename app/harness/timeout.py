"""
Harness 超时控制模块

提供任务超时检测、重试和进度同步功能。
"""

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, TypeVar

__all__ = ["TimeoutState", "TimeoutConfig", "TimeoutController", "ProgressReporter"]


class TimeoutState(Enum):
    """超时状态"""

    PENDING = "pending"  # 等待中
    RUNNING = "running"  # 运行中
    COMPLETED = "completed"  # 完成
    TIMEOUT = "timeout"  # 超时
    CANCELLED = "cancelled"  # 已取消


@dataclass
class TimeoutConfig:
    """超时配置"""

    timeout_seconds: float = 300.0  # 超时时间 (秒)
    progress_interval: float = 30.0  # 进度同步间隔 (秒)
    max_retries: int = 2  # 最大重试次数
    retry_delay: float = 1.0  # 重试延迟 (秒)
    exponential_backoff: bool = False  # 是否使用指数退避


@dataclass
class TaskResult:
    """任务执行结果"""

    success: bool
    data: Optional[Any] = None
    error: Optional[str] = None
    execution_time: float = 0.0
    retry_count: int = 0
    state: TimeoutState = TimeoutState.COMPLETED


T = TypeVar("T")


class TimeoutController:
    """
    超时控制器

    管理任务执行，提供超时检测、重试和进度同步功能。

    使用示例:
        controller = TimeoutController(TimeoutConfig(timeout_seconds=60))

        async def my_task():
            # 执行任务
            return {"result": "success"}

        result = await controller.execute(my_task)
        if result.success:
            print(f"Task completed: {result.data}")
        else:
            print(f"Task failed: {result.error}")
    """

    def __init__(self, config: Optional[TimeoutConfig] = None):
        """
        初始化超时控制器

        Args:
            config: 超时配置
        """
        self.config = config or TimeoutConfig()
        self._state = TimeoutState.PENDING
        self._start_time: Optional[float] = None
        self._end_time: Optional[float] = None
        self._task: Optional[asyncio.Task] = None
        self._progress_callbacks: List[Callable[[Dict[str, Any]], None]] = []
        self._last_progress_time: float = 0.0

    def add_progress_callback(
        self, callback: Callable[[Dict[str, Any]], None]
    ) -> None:
        """
        添加进度回调

        Args:
            callback: 进度回调函数，接收进度字典
        """
        self._progress_callbacks.append(callback)

    def remove_progress_callback(
        self, callback: Callable[[Dict[str, Any]], None]
    ) -> None:
        """移除进度回调"""
        if callback in self._progress_callbacks:
            self._progress_callbacks.remove(callback)

    async def _report_progress(self, progress: Dict[str, Any]) -> None:
        """
        报告进度

        Args:
            progress: 进度信息
        """
        current_time = time.time()
        if current_time - self._last_progress_time >= self.config.progress_interval:
            for callback in self._progress_callbacks:
                try:
                    callback(progress)
                except Exception:
                    pass  # 忽略回调错误
            self._last_progress_time = current_time

    async def _execute_with_timeout(
        self, func: Callable[..., T], *args, **kwargs
    ) -> T:
        """
        带超时的执行

        Args:
            func: 要执行的函数
            *args: 位置参数
            **kwargs: 关键字参数

        Returns:
            函数执行结果

        Raises:
            asyncio.TimeoutError: 超时时抛出
        """
        return await asyncio.wait_for(func(*args, **kwargs), self.config.timeout_seconds)

    async def execute(
        self,
        func: Callable[..., T],
        *args,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        **kwargs,
    ) -> TaskResult:
        """
        执行任务 (带超时和重试)

        Args:
            func: 要执行的函数 (可以是 async 或普通函数)
            *args: 位置参数
            progress_callback: 进度回调函数
            **kwargs: 关键字参数

        Returns:
            TaskResult 执行结果
        """
        if progress_callback:
            self.add_progress_callback(progress_callback)

        self._state = TimeoutState.RUNNING
        self._start_time = time.time()
        self._last_progress_time = self._start_time

        last_error: Optional[str] = None
        retry_count = 0
        retry_delay = self.config.retry_delay

        for attempt in range(self.config.max_retries + 1):
            try:
                # 报告进度
                await self._report_progress(
                    {
                        "state": "running",
                        "attempt": attempt + 1,
                        "max_attempts": self.config.max_retries + 1,
                        "elapsed_time": time.time() - self._start_time,
                    }
                )

                # 执行任务
                if asyncio.iscoroutinefunction(func):
                    result = await self._execute_with_timeout(func, *args, **kwargs)
                else:
                    # 在线程池中执行同步函数
                    loop = asyncio.get_event_loop()
                    result = await asyncio.wait_for(
                        loop.run_in_executor(None, lambda: func(*args, **kwargs)),
                        self.config.timeout_seconds,
                    )

                # 执行成功
                self._state = TimeoutState.COMPLETED
                self._end_time = time.time()

                return TaskResult(
                    success=True,
                    data=result,
                    execution_time=self._end_time - self._start_time,
                    retry_count=retry_count,
                    state=self._state,
                )

            except asyncio.TimeoutError as e:
                last_error = f"Timeout after {self.config.timeout_seconds}s"
                self._state = TimeoutState.TIMEOUT

            except Exception as e:
                last_error = str(e)
                self._state = TimeoutState.RUNNING  # 保持运行状态以进行重试

            # 重试逻辑
            if attempt < self.config.max_retries:
                retry_count += 1

                # 指数退避
                if self.config.exponential_backoff:
                    retry_delay *= 2

                await asyncio.sleep(retry_delay)

        # 所有重试失败
        self._state = TimeoutState.TIMEOUT if "Timeout" in str(last_error) else TimeoutState.RUNNING
        self._end_time = time.time()

        return TaskResult(
            success=False,
            error=last_error,
            execution_time=self._end_time - self._start_time,
            retry_count=retry_count,
            state=self._state,
        )

    def cancel(self) -> None:
        """取消当前任务"""
        self._state = TimeoutState.CANCELLED
        if self._task:
            self._task.cancel()

    @property
    def state(self) -> TimeoutState:
        """获取当前状态"""
        return self._state

    @property
    def elapsed_time(self) -> float:
        """获取已用时间 (秒)"""
        if self._start_time is None:
            return 0.0
        end_time = self._end_time or time.time()
        return end_time - self._start_time

    @property
    def remaining_time(self) -> float:
        """获取剩余时间 (秒)"""
        if self._start_time is None:
            return self.config.timeout_seconds
        return max(0.0, self.config.timeout_seconds - self.elapsed_time)

    def is_running(self) -> bool:
        """检查是否正在运行"""
        return self._state == TimeoutState.RUNNING

    def is_completed(self) -> bool:
        """检查是否已完成"""
        return self._state == TimeoutState.COMPLETED

    def is_timeout(self) -> bool:
        """检查是否超时"""
        return self._state == TimeoutState.TIMEOUT


class ProgressReporter:
    """
    进度报告器

    用于定期报告任务执行进度。
    """

    def __init__(
        self,
        task_name: str,
        interval: float = 30.0,
        callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        """
        初始化进度报告器

        Args:
            task_name: 任务名称
            interval: 报告间隔 (秒)
            callback: 进度回调函数
        """
        self.task_name = task_name
        self.interval = interval
        self.callback = callback
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._progress_data: Dict[str, Any] = {}
        self._start_time: Optional[float] = None

    def update(self, **kwargs) -> None:
        """更新进度数据"""
        self._progress_data.update(kwargs)

    async def start(self) -> None:
        """启动进度报告"""
        self._running = True
        self._start_time = time.time()
        self._task = asyncio.create_task(self._report_loop())

    async def stop(self) -> None:
        """停止进度报告"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _report_loop(self) -> None:
        """进度报告循环"""
        while self._running:
            await asyncio.sleep(self.interval)

            progress = {
                "task_name": self.task_name,
                "elapsed_time": time.time() - self._start_time if self._start_time else 0,
                **self._progress_data,
            }

            if self.callback:
                try:
                    self.callback(progress)
                except Exception:
                    pass  # 忽略回调错误
