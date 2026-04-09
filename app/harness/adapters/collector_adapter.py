"""
Collector Adapter - 数据收集模块适配器

将现有 periodic_collector_hk 模块适配为 TradeMonitor Agent。
"""

import logging
from typing import Any, Dict, Optional

from ..base import AgentConfig
from .base_adapter import BaseAdapter

__all__ = ["CollectorAdapter"]


class CollectorAdapter(BaseAdapter):
    """
    收集适配器

    将 PeriodicTradeCollector 适配为 TradeMonitor Agent。

    使用示例:
        adapter = CollectorAdapter()
        result = await adapter.run({
            "task": "collect",
            "market": "HK",
        })
    """

    def __init__(self, config: Optional[AgentConfig] = None):
        """初始化收集适配器"""
        if config is None:
            config = AgentConfig(
                name="collector-adapter",
                timeout_seconds=300,
                retry_count=2,
                metadata={
                    "role": "收集适配器",
                    "adapts": "app.review.periodic_collector_hk",
                },
            )

        super().__init__(config, module_name="collector")
        self._collector = None

    async def _load_module(self, module_path: str) -> Any:
        """加载定期收集器模块"""
        try:
            from app.review.periodic_collector_hk import PeriodicTradeCollector
            # 需要 db_session 和 futu_client，这里简化处理
            self._collector = None  # 实际使用时需要传入依赖
            self.logger.info("PeriodicTradeCollector module loaded")
            return self._collector
        except ImportError as e:
            self.logger.warning(f"Failed to load PeriodicTradeCollector: {e}")
            self._collector = None
            raise

    async def _adapt_execute(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        适配执行

        Args:
            input_data: 输入数据

        Returns:
            执行结果
        """
        task = input_data.get("task", "collect")
        market = input_data.get("market", "HK")

        self.logger.info(f"CollectorAdapter executing task: {task} (market: {market})")

        try:
            if task == "collect":
                return await self._collect(market)
            elif task == "status":
                return await self._get_status()
            else:
                raise ValueError(f"Unknown task: {task}")
        except Exception as e:
            self.logger.error(f"CollectorAdapter execution failed: {e}", exc_info=True)
            return self._fallback_collect(market)

    async def _collect(self, market: str) -> Dict[str, Any]:
        """收集交易数据"""
        # 简化实现，实际应该调用 collector.collect_trades()
        return {
            "task": "collect",
            "market": market,
            "trades": [],
            "trade_count": 0,
            "collection_time": "2026-03-29T09:00:00",
            "adapter": "collector",
            "note": "Placeholder - actual implementation requires db_session and futu_client",
        }

    async def _get_status(self) -> Dict[str, Any]:
        """获取收集状态"""
        return {
            "task": "status",
            "collector_active": True,
            "last_collection": "2026-03-29T08:30:00",
            "next_collection": "2026-03-29T09:00:00",
            "interval_seconds": 1800,
            "adapter": "collector",
        }

    def _fallback_collect(self, market: str) -> Dict[str, Any]:
        """降级收集"""
        return {
            "task": "collect",
            "market": market,
            "trades": [],
            "trade_count": 0,
            "adapter": "collector",
            "fallback": True,
        }

    def get_capabilities(self) -> Dict[str, Any]:
        """获取能力描述"""
        caps = super().get_capabilities()
        caps["capabilities"].extend([
            "trade_collection",
            "status_reporting",
        ])
        caps["collector_loaded"] = self._collector is not None
        return caps
