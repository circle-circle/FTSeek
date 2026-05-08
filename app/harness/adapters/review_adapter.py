"""
Review Adapter - 复盘模块适配器 (2026-05-04 重构)

将 HKEODReview 全链路复盘管线适配为 Harness Agent。
管线: HKEODCollector → HKAttributionRunner → HKMemoryInjector → Summary → Notify
"""

import logging
from typing import Any, Dict, Optional

from ..base import AgentConfig
from .base_adapter import BaseAdapter

__all__ = ["ReviewAdapter", "HKEODReviewAdapter"]


class HKEODReviewAdapter(BaseAdapter):
    """
    HK EOD 复盘适配器。

    将 HKEODReview 5步管线适配为 Harness Agent:
    1. HKEODCollector.collect() — 收集交易/持仓/作战计划/A股数据
    2. HKAttributionRunner.run() — 多维度归因分析
    3. HKMemoryInjector.update_memory() — 更新记忆/教训库
    4. _generate_summary() — 生成复盘摘要
    5. _send_notification() — 推送钉钉通知

    使用示例:
        adapter = HKEODReviewAdapter(notifier=dingtalk_notifier)
        result = await adapter.run({
            "task": "review",
            "date": "2026-04-22",
            "market": "HK",
        })
    """

    def __init__(self, config: Optional[AgentConfig] = None, notifier: Any = None):
        if config is None:
            config = AgentConfig(
                name="hk-eod-review",
                timeout_seconds=600,
                retry_count=1,
                metadata={
                    "role": "HK EOD 复盘适配器",
                    "adapts": "app.review.hk_eod_review",
                    "pipeline": ["collect", "attribute", "inject_memory", "summarize", "notify"],
                },
            )

        super().__init__(config, module_name="hk_eod_review")
        self._review = None
        self._notifier = notifier

    async def _load_module(self, module_path: str) -> Any:
        try:
            from app.review.hk_eod_review import HKEODReview
            self._review = HKEODReview(quote_ctx=None, notifier=self._notifier)
            self.logger.info("HKEODReview loaded (5-step EOD pipeline)")
            return self._review
        except ImportError as e:
            self.logger.warning(f"Failed to load HKEODReview: {e}")
            self._review = None
            raise

    async def _adapt_execute(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        task = input_data.get("task", "review")
        date = input_data.get("date")
        market = input_data.get("market", "HK")

        self.logger.info(f"HKEODReviewAdapter executing: {task} date={date} market={market}")

        if not self._review:
            self.logger.warning("HKEODReview not loaded, returning fallback")
            return self._fallback_review(date, market)

        try:
            if task in ("review", "analyze", "eod_review"):
                result = self._review.run(trade_date=date)
                return {
                    "task": "eod_review",
                    "date": date,
                    "market": market,
                    "status": result.get("status", "unknown"),
                    "trades_reviewed": result.get("trades_reviewed", 0),
                    "lessons_generated": result.get("lessons_generated", 0),
                    "memory_updated": result.get("memory_updated", False),
                    "notification_sent": result.get("notification_sent", False),
                    "summary": result.get("summary", ""),
                    "errors": result.get("errors", []),
                    "adapter": "hk_eod_review",
                }
            elif task == "status":
                return {
                    "task": "status",
                    "review_loaded": self._review is not None,
                    "adapter": "hk_eod_review",
                }
            else:
                raise ValueError(f"Unknown task: {task}")
        except Exception as e:
            self.logger.error(f"HKEODReviewAdapter failed: {e}", exc_info=True)
            return self._fallback_review(date, market)

    def _fallback_review(self, date: Optional[str], market: str) -> Dict[str, Any]:
        return {
            "task": "eod_review",
            "date": date,
            "market": market,
            "status": "fallback",
            "trades_reviewed": 0,
            "lessons_generated": 0,
            "memory_updated": False,
            "notification_sent": False,
            "summary": "",
            "errors": ["HKEODReview unavailable"],
            "adapter": "hk_eod_review",
            "fallback": True,
        }

    def get_capabilities(self) -> Dict[str, Any]:
        caps = super().get_capabilities()
        caps["capabilities"].extend([
            "eod_review",
            "trade_attribution",
            "memory_injection",
            "dingtalk_notification",
            "summary_generation",
        ])
        caps["review_loaded"] = self._review is not None
        return caps


# 保留旧 ReviewAdapter 用于兼容
class ReviewAdapter(BaseAdapter):
    """复盘适配器 (兼容保留) — 建议使用 HKEODReviewAdapter"""

    def __init__(self, config: Optional[AgentConfig] = None):
        if config is None:
            config = AgentConfig(
                name="review-adapter",
                timeout_seconds=300,
                retry_count=2,
                metadata={"role": "复盘适配器", "adapts": "app.review.batch_processor_hk"},
            )
        super().__init__(config, module_name="review")
        self._processor = None

    async def _load_module(self, module_path: str) -> Any:
        try:
            from app.review.batch_processor_hk import BatchReviewProcessor
            self._processor = BatchReviewProcessor()
            self.logger.info("BatchReviewProcessor loaded")
            return self._processor
        except ImportError as e:
            self.logger.warning(f"Failed to load BatchReviewProcessor: {e}")
            self._processor = None
            raise

    async def _adapt_execute(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        task = input_data.get("task", "analyze")
        date = input_data.get("date")
        stock = input_data.get("stock", "ALL")

        if not self._processor:
            return {"task": task, "date": date, "error": "processor not available", "fallback": True}

        if task == "analyze":
            try:
                analysis = await self._processor.analyze_trades(date, stock)
                return {"task": "analyze", "date": date, "stock": stock, "analysis": analysis}
            except Exception as e:
                return {"task": "analyze", "date": date, "error": str(e), "fallback": True}

        return {"task": task, "date": date, "error": f"unknown task: {task}"}
