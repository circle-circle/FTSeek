"""
Review Adapter - 复盘模块适配器

将现有 batch_processor_hk 模块适配为 Strategist Agent。
"""

import logging
from typing import Any, Dict, Optional

from ..base import AgentConfig
from .base_adapter import BaseAdapter

__all__ = ["ReviewAdapter"]


class ReviewAdapter(BaseAdapter):
    """
    复盘适配器

    将 BatchReviewProcessor 适配为 Strategist Agent。

    使用示例:
        adapter = ReviewAdapter()
        result = await adapter.run({
            "task": "analyze",
            "date": "2026-03-29",
            "stock": "HK.09992",
        })
    """

    def __init__(self, config: Optional[AgentConfig] = None):
        """初始化复盘适配器"""
        if config is None:
            config = AgentConfig(
                name="review-adapter",
                timeout_seconds=300,
                retry_count=2,
                metadata={
                    "role": "复盘适配器",
                    "adapts": "app.review.batch_processor_hk",
                },
            )

        super().__init__(config, module_name="review")
        self._processor = None

    async def _load_module(self, module_path: str) -> Any:
        """加载批量处理器模块"""
        try:
            from app.review.batch_processor_hk import BatchReviewProcessor
            self._processor = BatchReviewProcessor()
            self.logger.info("BatchReviewProcessor loaded successfully")
            return self._processor
        except ImportError as e:
            self.logger.warning(f"Failed to load BatchReviewProcessor: {e}")
            self._processor = None
            raise

    async def _adapt_execute(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        适配执行

        Args:
            input_data: 输入数据

        Returns:
            执行结果
        """
        task = input_data.get("task", "analyze")
        date = input_data.get("date")
        stock = input_data.get("stock", "ALL")

        self.logger.info(f"ReviewAdapter executing task: {task} (date: {date}, stock: {stock})")

        if not self._processor:
            self.logger.warning("Processor not loaded, using fallback")
            return self._fallback_analysis(date, stock)

        try:
            if task == "analyze":
                return await self._analyze(date, stock)
            elif task == "daily":
                return await self._daily_review(date)
            elif task == "weekly":
                return await self._weekly_review(date)
            else:
                raise ValueError(f"Unknown task: {task}")
        except Exception as e:
            self.logger.error(f"ReviewAdapter execution failed: {e}", exc_info=True)
            return self._fallback_analysis(date, stock)

    async def _analyze(self, date: Optional[str], stock: str) -> Dict[str, Any]:
        """分析交易"""
        if not date:
            return {"error": "Date is required for analysis"}

        # 调用现有批量处理器
        try:
            analysis = await self._processor.analyze_trades(date, stock)
            return {
                "task": "analyze",
                "date": date,
                "stock": stock,
                "analysis": analysis,
                "adapter": "review",
            }
        except Exception as e:
            self.logger.error(f"Analysis failed: {e}")
            return self._fallback_analysis(date, stock)

    async def _daily_review(self, date: str) -> Dict[str, Any]:
        """每日复盘"""
        try:
            report = await self._processor.generate_daily_report(date)
            return {
                "task": "daily_review",
                "date": date,
                "report": report,
                "adapter": "review",
            }
        except Exception as e:
            self.logger.error(f"Daily review failed: {e}")
            return {"error": str(e), "task": "daily_review", "date": date}

    async def _weekly_review(self, date: str) -> Dict[str, Any]:
        """每周复盘"""
        try:
            report = await self._processor.generate_weekly_report(date)
            return {
                "task": "weekly_review",
                "date": date,
                "report": report,
                "adapter": "review",
            }
        except Exception as e:
            self.logger.error(f"Weekly review failed: {e}")
            return {"error": str(e), "task": "weekly_review", "date": date}

    def _fallback_analysis(self, date: Optional[str], stock: str) -> Dict[str, Any]:
        """降级分析"""
        return {
            "task": "analyze",
            "date": date,
            "stock": stock,
            "analysis": {
                "win_rate": 0.0,
                "total_trades": 0,
                "total_pnl": 0.0,
                "note": "Fallback analysis (processor not available)",
            },
            "adapter": "review",
            "fallback": True,
        }

    def get_capabilities(self) -> Dict[str, Any]:
        """获取能力描述"""
        caps = super().get_capabilities()
        caps["capabilities"].extend([
            "trade_analysis",
            "daily_review",
            "weekly_review",
            "report_generation",
        ])
        caps["processor_loaded"] = self._processor is not None
        return caps
