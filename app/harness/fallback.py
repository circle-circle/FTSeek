"""
Harness 降级策略模块

提供主模型失败时的兜底切换和降级执行功能。
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

__all__ = [
    "FallbackStrategy",
    "FallbackConfig",
    "FallbackResult",
    "ModelRouter",
]


class FallbackReason(Enum):
    """降级原因"""

    TIMEOUT = "timeout"  # 超时
    ERROR = "error"  # 错误
    LOW_CONFIDENCE = "low_confidence"  # 置信度低
    MANUAL = "manual"  # 手动切换


@dataclass
class FallbackConfig:
    """降级配置"""

    primary_model: str = "primary"  # 主模型名称
    fallback_models: List[str] = field(default_factory=list)  # 兜底模型列表
    switch_threshold: int = 2  # 切换阈值 (连续失败次数)
    confidence_threshold: float = 0.5  # 置信度阈值
    cooldown_seconds: float = 300.0  # 冷却时间 (秒)
    auto_switch: bool = True  # 是否自动切换


@dataclass
class FallbackResult:
    """降级执行结果"""

    success: bool
    model_used: str  # 使用的模型
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    execution_time: float = 0.0
    fallback_reason: Optional[FallbackReason] = None
    attempt_count: int = 1
    metadata: Dict[str, Any] = field(default_factory=dict)


class FallbackStrategy:
    """
    降级策略

    管理主模型和兜底模型的切换逻辑。

    使用示例:
        strategy = FallbackStrategy(FallbackConfig(
            primary_model="deepseek-reasoner",
            fallback_models=["qwen3.5-plus"],
            switch_threshold=2,
        ))

        async def execute_with_model(model_name: str, input_data: Dict) -> Dict:
            # 执行模型调用
            return {"result": "success"}

        result = await strategy.execute(execute_with_model, {"key": "value"})
        print(f"Used model: {result.model_used}")
    """

    def __init__(self, config: Optional[FallbackConfig] = None):
        """
        初始化降级策略

        Args:
            config: 降级配置
        """
        self.config = config or FallbackConfig()
        self._current_model = self.config.primary_model
        self._failure_counts: Dict[str, int] = {}
        self._last_failure_time: Dict[str, float] = {}
        self._is_fallback_mode = False
        self._logger = logging.getLogger("harness.fallback")

    @property
    def current_model(self) -> str:
        """获取当前模型"""
        return self._current_model

    @property
    def is_fallback_mode(self) -> bool:
        """检查是否处于兜底模式"""
        return self._is_fallback_mode

    def record_failure(self, model: str, error: str) -> None:
        """
        记录模型失败

        Args:
            model: 模型名称
            error: 错误信息
        """
        self._failure_counts[model] = self._failure_counts.get(model, 0) + 1
        self._last_failure_time[model] = datetime.now().timestamp()

        self._logger.warning(
            f"Model {model} failed ({self._failure_counts[model]}/"
            f"{self.config.switch_threshold}): {error}"
        )

        # 检查是否需要切换
        if (
            self.config.auto_switch
            and self._failure_counts[model] >= self.config.switch_threshold
            and model == self.config.primary_model
        ):
            self._switch_to_fallback()

    def record_success(self, model: str) -> None:
        """
        记录模型成功

        Args:
            model: 模型名称
        """
        # 重置失败计数
        self._failure_counts[model] = 0

    def _switch_to_fallback(self) -> None:
        """切换到兜底模型"""
        if self.config.fallback_models:
            old_model = self._current_model
            self._current_model = self.config.fallback_models[0]
            self._is_fallback_mode = True

            self._logger.warning(
                f"Switching from {old_model} to {self._current_model} "
                f"(fallback mode activated)"
            )

    def reset(self) -> None:
        """重置状态"""
        self._current_model = self.config.primary_model
        self._is_fallback_mode = False
        self._failure_counts.clear()
        self._logger.info("Fallback strategy reset")

    async def execute(
        self,
        executor: Callable[[str, Dict[str, Any]], Any],
        input_data: Dict[str, Any],
    ) -> FallbackResult:
        """
        执行降级策略

        Args:
            executor: 执行函数 (接收模型名称和输入数据)
            input_data: 输入数据

        Returns:
            FallbackResult 执行结果
        """
        models_to_try = [self._current_model]

        # 如果不是兜底模式，添加兜底模型列表
        if not self._is_fallback_mode and self.config.fallback_models:
            models_to_try.extend(self.config.fallback_models)

        last_error: Optional[str] = None
        attempt_count = 0

        for model in models_to_try:
            attempt_count += 1
            start_time = datetime.now()

            try:
                self._logger.info(f"Executing with model: {model}")

                # 执行
                if asyncio.iscoroutinefunction(executor):
                    result_data = await executor(model, input_data)
                else:
                    result_data = executor(model, input_data)

                # 成功
                self.record_success(model)
                execution_time = (datetime.now() - start_time).total_seconds()

                return FallbackResult(
                    success=True,
                    model_used=model,
                    data=result_data,
                    execution_time=execution_time,
                    attempt_count=attempt_count,
                )

            except Exception as e:
                last_error = str(e)
                self.record_failure(model, last_error)
                self._logger.error(f"Model {model} failed: {last_error}")

        # 所有模型都失败
        execution_time = (datetime.now() - start_time).total_seconds()
        fallback_reason = FallbackReason.ERROR
        if last_error and "timeout" in last_error.lower():
            fallback_reason = FallbackReason.TIMEOUT

        return FallbackResult(
            success=False,
            model_used=self._current_model,
            error=last_error,
            execution_time=execution_time,
            fallback_reason=fallback_reason,
            attempt_count=attempt_count,
        )

    def get_status(self) -> Dict[str, Any]:
        """获取状态"""
        return {
            "current_model": self._current_model,
            "is_fallback_mode": self._is_fallback_mode,
            "failure_counts": self._failure_counts.copy(),
            "primary_model": self.config.primary_model,
            "fallback_models": self.config.fallback_models,
        }


class ModelRouter:
    """
    模型路由器

    根据任务类型和负载情况路由到合适的模型。
    """

    def __init__(self):
        """初始化模型路由器"""
        self._model_configs: Dict[str, Dict[str, Any]] = {}
        self._model_load: Dict[str, int] = {}
        self._logger = logging.getLogger("harness.router")

    def register_model(
        self,
        model_name: str,
        capabilities: List[str],
        max_concurrent: int = 10,
        priority: int = 1,
    ) -> None:
        """
        注册模型

        Args:
            model_name: 模型名称
            capabilities: 能力列表
            max_concurrent: 最大并发数
            priority: 优先级
        """
        self._model_configs[model_name] = {
            "capabilities": capabilities,
            "max_concurrent": max_concurrent,
            "priority": priority,
        }
        self._model_load[model_name] = 0
        self._logger.info(f"Model registered: {model_name} ({capabilities})")

    def select_model(
        self, required_capabilities: List[str]
    ) -> Optional[str]:
        """
        选择模型

        Args:
            required_capabilities: 所需能力列表

        Returns:
            模型名称，如果没有合适模型则返回 None
        """
        candidates = []

        for model_name, config in self._model_configs.items():
            # 检查能力匹配
            has_all_caps = all(
                cap in config["capabilities"] for cap in required_capabilities
            )
            if not has_all_caps:
                continue

            # 检查负载
            if self._model_load[model_name] >= config["max_concurrent"]:
                continue

            candidates.append((model_name, config["priority"]))

        if not candidates:
            return None

        # 选择优先级最高的
        candidates.sort(key=lambda x: x[1], reverse=True)
        selected = candidates[0][0]

        # 增加负载
        self._model_load[selected] += 1

        self._logger.debug(f"Selected model: {selected}")
        return selected

    def release_model(self, model_name: str) -> None:
        """
        释放模型负载

        Args:
            model_name: 模型名称
        """
        if model_name in self._model_load:
            self._model_load[model_name] = max(0, self._model_load[model_name] - 1)

    def get_available_models(self) -> List[str]:
        """获取可用模型列表"""
        return list(self._model_configs.keys())

    def get_model_info(self, model_name: str) -> Optional[Dict[str, Any]]:
        """获取模型信息"""
        config = self._model_configs.get(model_name)
        if not config:
            return None

        return {
            "name": model_name,
            **config,
            "current_load": self._model_load.get(model_name, 0),
        }
