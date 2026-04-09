"""
Adapter 基类

提供将现有模块适配为 Harness Agent 的基础功能。
"""

import logging
from typing import Any, Dict, Optional

from ..base import Agent, AgentConfig, AgentResult

__all__ = ["BaseAdapter"]


class BaseAdapter(Agent):
    """
    Adapter 基类

    用于将现有模块适配为 Harness Agent。

    使用示例:
        class ReviewAdapter(BaseAdapter):
            def __init__(self):
                super().__init__(AgentConfig(name="review-adapter"))
                self.processor = BatchProcessor()

            async def _adapt_execute(self, input_data: Dict) -> Dict:
                # 调用现有模块
                result = self.processor.analyze(input_data)
                return result

        adapter = ReviewAdapter()
        result = await adapter.run({"date": "2026-03-29"})
    """

    def __init__(
        self,
        config: Optional[AgentConfig] = None,
        module_name: str = "unknown",
    ):
        """
        初始化 Adapter

        Args:
            config: Agent 配置
            module_name: 被适配的模块名称
        """
        if config is None:
            config = AgentConfig(name=f"adapter-{module_name}")

        super().__init__(config)
        self.module_name = module_name
        self._adapted_module: Optional[Any] = None

    async def _load_module(self, module_path: str) -> Any:
        """
        动态加载模块

        Args:
            module_path: 模块路径

        Returns:
            模块对象
        """
        import importlib

        try:
            module = importlib.import_module(module_path)
            self._adapted_module = module
            self.logger.info(f"Module loaded: {module_path}")
            return module
        except ImportError as e:
            self.logger.error(f"Failed to load module {module_path}: {e}")
            raise

    def get_adapted_module(self) -> Optional[Any]:
        """获取已加载的模块"""
        return self._adapted_module

    async def _adapt_execute(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        适配执行 (子类必须实现)

        子类必须实现此方法，调用被适配模块的功能。

        Args:
            input_data: 输入数据

        Returns:
            执行结果字典
        """
        raise NotImplementedError("Subclasses must implement _adapt_execute")

    async def execute(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行适配任务

        Args:
            input_data: 输入数据

        Returns:
            执行结果字典
        """
        self.logger.info(f"Adapter {self.name} executing for module: {self.module_name}")

        try:
            # 调用子类的适配方法
            result = await self._adapt_execute(input_data)

            # 标准化结果
            if not isinstance(result, dict):
                result = {"data": result}

            result["_adapter"] = self.module_name
            return result

        except Exception as e:
            self.logger.error(f"Adapter {self.name} execution failed: {e}", exc_info=True)
            raise

    def get_capabilities(self) -> Dict[str, Any]:
        """获取能力描述"""
        base_caps = super().get_capabilities()
        base_caps["adapter"] = True
        base_caps["module_name"] = self.module_name
        return base_caps
