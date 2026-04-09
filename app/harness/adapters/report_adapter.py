"""
Report Adapter - 报告生成模块适配器

将现有 daily_review 模块适配为 ContextEngineer Agent。
"""

import logging
from typing import Any, Dict, Optional

from ..base import AgentConfig
from .base_adapter import BaseAdapter

__all__ = ["ReportAdapter"]


class ReportAdapter(BaseAdapter):
    """
    报告适配器

    将 DailyReviewGenerator 适配为 ContextEngineer Agent。

    使用示例:
        adapter = ReportAdapter()
        result = await adapter.run({
            "task": "generate",
            "date": "2026-03-29",
            "report_type": "daily",
        })
    """

    def __init__(self, config: Optional[AgentConfig] = None):
        """初始化报告适配器"""
        if config is None:
            config = AgentConfig(
                name="report-adapter",
                timeout_seconds=300,
                retry_count=2,
                metadata={
                    "role": "报告适配器",
                    "adapts": "app.review.daily_review",
                },
            )

        super().__init__(config, module_name="report")
        self._report_generator = None

    async def _load_module(self, module_path: str) -> Any:
        """加载报告生成模块"""
        try:
            from app.review.daily_review import DailyReviewGenerator
            self._report_generator = DailyReviewGenerator()
            self.logger.info("DailyReviewGenerator loaded successfully")
            return self._report_generator
        except ImportError as e:
            self.logger.warning(f"Failed to load DailyReviewGenerator: {e}")
            self._report_generator = None
            raise

    async def _adapt_execute(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        适配执行

        Args:
            input_data: 输入数据

        Returns:
            执行结果
        """
        task = input_data.get("task", "generate")
        report_type = input_data.get("report_type", "daily")
        date = input_data.get("date")

        self.logger.info(f"ReportAdapter executing task: {task} (type: {report_type}, date: {date})")

        if not self._report_generator:
            self.logger.warning("ReportGenerator not loaded, using fallback")
            return self._fallback_report(report_type, date)

        try:
            if task == "generate":
                return await self._generate(report_type, date)
            elif task == "summarize":
                return await self._summarize(date)
            else:
                raise ValueError(f"Unknown task: {task}")
        except Exception as e:
            self.logger.error(f"ReportAdapter execution failed: {e}", exc_info=True)
            return self._fallback_report(report_type, date)

    async def _generate(self, report_type: str, date: str) -> Dict[str, Any]:
        """生成报告"""
        try:
            if report_type == "daily":
                report = await self._report_generator.generate_daily(date)
            elif report_type == "weekly":
                report = await self._report_generator.generate_weekly(date)
            elif report_type == "monthly":
                report = await self._report_generator.generate_monthly(date)
            else:
                raise ValueError(f"Unknown report type: {report_type}")

            return {
                "task": "generate",
                "report_type": report_type,
                "date": date,
                "report": report,
                "adapter": "report",
            }
        except Exception as e:
            self.logger.error(f"Report generation failed: {e}")
            return self._fallback_report(report_type, date)

    async def _summarize(self, date: str) -> Dict[str, Any]:
        """生成摘要"""
        try:
            summary = await self._report_generator.summarize(date)
            return {
                "task": "summarize",
                "date": date,
                "summary": summary,
                "adapter": "report",
            }
        except Exception as e:
            self.logger.error(f"Summarization failed: {e}")
            return {"error": str(e), "task": "summarize", "date": date}

    def _fallback_report(self, report_type: str, date: Optional[str]) -> Dict[str, Any]:
        """降级报告"""
        return {
            "task": "generate",
            "report_type": report_type,
            "date": date,
            "report": {
                "title": f"{report_type.title()} Review Report",
                "date": date,
                "summary": "No data available",
                "note": "Fallback report (report generator not available)",
            },
            "adapter": "report",
            "fallback": True,
        }

    def get_capabilities(self) -> Dict[str, Any]:
        """获取能力描述"""
        caps = super().get_capabilities()
        caps["capabilities"].extend([
            "daily_report",
            "weekly_report",
            "monthly_report",
            "summarization",
        ])
        caps["report_generator_loaded"] = self._report_generator is not None
        return caps
