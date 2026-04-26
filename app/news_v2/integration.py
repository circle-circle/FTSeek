"""
FTSeek 新闻管线 V2 — 集成适配器 (混合模式)

关键修复: collector.record_received 对所有项目调用 (通过+拒绝)
"""

import asyncio
import logging
import time
from typing import Optional, Dict

logger = logging.getLogger("ftseek.news_v2.integration")


class NewsV2Integration:
    def __init__(self,
                 dingtalk_notifier=None,
                 llm_strategy_engine=None,
                 signal_store=None,
                 feishu_notifier=None,
                 mode: str = "observe",
                 collector=None):
        self.dingtalk = dingtalk_notifier
        self.llm_engine = llm_strategy_engine
        self.signal_store = signal_store
        self.feishu = feishu_notifier
        self.mode = mode
        self.collector = collector

        self._stats = {
            "total_processed": 0,
            "total_pushed": 0,
            "total_analyzed": 0,
            "total_signals": 0,
            "errors": 0,
        }

    def attach_to_pipeline(self, pipeline):
        pipeline.on_news_passed = self._on_news_passed
        pipeline.on_geo_event = self._on_geo_event
        pipeline.on_immediate = self._on_immediate
        logger.info("[V2 Integration] 已挂载 (mode=%s)", self.mode)

    def _on_news_passed(self, item):
        start = time.time()
        self._stats["total_processed"] += 1

        # 始终记录 (通过)
        if self.collector:
            self.collector.record_received(item.to_dict(), passed=True)

        try:
            if self.mode == "off":
                return

            if self.mode == "active" and (item.raw_score >= 0.4 or item.is_immediate()):
                self._push_notification(item)
                self._stats["total_pushed"] += 1

            if self.mode == "active" and (item.is_immediate() or item.raw_score >= 0.5):
                asyncio.create_task(self._trigger_llm_analysis(item))

            if self.mode == "observe":
                logger.info(
                    "[V2 Observe] %s | score=%.2f | %s",
                    item.source_name, item.raw_score, item.title[:50]
                )

        except Exception as e:
            self._stats["errors"] += 1
            if self.collector:
                self.collector.record_error(str(e))
            logger.error("[V2 Integration] 异常: %s", e)
        finally:
            if self.collector:
                self.collector.record_processing_time((time.time() - start) * 1000)

    def _on_geo_event(self, item):
        if self.collector:
            self.collector.record_received(item.to_dict(), passed=True)
        logger.warning("[V2 GeoEvent] 🌍 %s | %s", item.source_name, item.title[:60])
        if self.mode == "active":
            self._push_notification(item, prefix="🌍 地缘事件")

    def _on_immediate(self, item):
        if self.collector:
            self.collector.record_received(item.to_dict(), passed=True)
        logger.warning("[V2 Immediate] ⚡ %s | %s", item.source_name, item.title[:60])
        if self.mode == "active":
            self._push_notification(item, prefix="⚡ 紧急")

    def _push_notification(self, item, prefix: str = None):
        try:
            tags = []
            if item.market_tags:
                tags.extend(item.market_tags[:2])
            if item.sectors:
                tags.extend(item.sectors[:2])
            tag_str = " ".join(f"[{t}]" for t in tags[:3])

            urgency_icon = {"IMMEDIATE": "⚡", "HIGH": "🔴", "NORMAL": "📰", "LOW": "📝"}.get(
                item.urgency, "📰"
            )

            header = f"{prefix} | {item.source_name}" if prefix else f"{urgency_icon} {item.source_name}"
            stock_str = ""
            if item.related_stocks:
                stock_str = "\n\n📊 相关: " + ", ".join(item.related_stocks[:5])

            message = f"### {header} {tag_str}\n\n**{item.title}**\n\n{item.content[:300]}{stock_str}\n\n_来源: {item.source_name} | 相关性: {item.raw_score:.0%}_"

            if self.dingtalk:
                try:
                    if hasattr(self.dingtalk, "send_text"):
                        self.dingtalk.send_text(message)
                    elif hasattr(self.dingtalk, "send_markdown"):
                        self.dingtalk.send_markdown(item.title, message)
                except Exception:
                    pass
            if self.feishu:
                try:
                    self.feishu.send_text(message)
                except Exception:
                    pass
        except Exception as e:
            logger.error("推送格式化失败: %s", e)

    async def _trigger_llm_analysis(self, item):
        if not self.llm_engine:
            return
        try:
            result = await self.llm_engine.analyze_news(
                news_title=item.title,
                news_content=item.content,
                news_source=item.source_name,
                pub_time=str(item.timestamp),
                event_name="地缘事件" if item.is_geo_event() else "",
                event_sector=", ".join(item.sectors) if item.sectors else "",
                static_logic="",
                related_stocks={c: c for c in item.related_stocks[:5]},
            )
            if result:
                self._stats["total_analyzed"] += 1
                if self.signal_store and result.stock_strategies:
                    for s in result.stock_strategies:
                        if s.direction not in ("NEUTRAL", "WATCH_ONLY", "SKIP"):
                            self._stats["total_signals"] += 1
                item.llm_analyzed = True
                item.llm_result = {"status": "ok"}
        except Exception as e:
            self._stats["errors"] += 1
            item.llm_result = {"status": "error", "error": str(e)}

    def get_stats(self) -> Dict:
        return self._stats.copy()

    def set_mode(self, mode: str):
        if mode in ("active", "observe", "off"):
            self.mode = mode
            logger.info("[V2 Integration] 模式 → %s", mode)
