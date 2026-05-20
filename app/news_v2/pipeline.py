"""
FTSeek-US 统一新闻管线 V2 — 主编排器

职责:
1. 管理所有新闻源 (采集 → 过滤 → 处理)
2. 质量门禁 (去重 → 相关性 → 重要性)
3. 双通道调度 (快速通道 vs 深度通道)
4. 状态监控与统计

用法:
    from app.news_v2.pipeline import NewsPipelineV2
    pipeline = NewsPipelineV2()
    await pipeline.run_once()  # 单次运行
    pipeline.start(interval=30)  # 持续运行
"""

import asyncio
import logging
import time
from typing import List, Dict, Optional, Callable
from datetime import datetime

from app.news_v2.models import UnifiedNewsItemV2, NewsUrgency
from app.news_v2.sources.base import SourceAdapter
from app.news_v2.quality.dedup_v2 import DedupEngineV2
from app.news_v2.quality.relevance_filter import RelevanceFilter

logger = logging.getLogger("ftseek_us.news_v2.pipeline")


class QualityGate:
    """
    质量门禁 — 四层过滤
    """
    
    def __init__(self, dedup: DedupEngineV2, relevance: RelevanceFilter,
                 max_age_minutes: int = 120, collector=None):
        self.dedup = dedup
        self.relevance = relevance
        self.max_age_minutes = max_age_minutes
        self._collector = collector
        self._stats = {
            "total_input": 0,
            "passed": 0,
            "rejected_duplicate": 0,
            "rejected_irrelevant": 0,
            "rejected_expired": 0,
            "rejected_unimportant": 0,
        }
    
    def evaluate(self, item: UnifiedNewsItemV2) -> tuple:
        """
        评估新闻是否通过质量门禁
        
        Returns:
            (passed: bool, reason: str)
        """
        self._stats["total_input"] += 1
        
        # Gate 1: 时效性
        if item.is_expired(self.max_age_minutes):
            self._stats["rejected_expired"] += 1
            if self._collector:
                self._collector.record_received(item.to_dict(), passed=False, reason="expired")
            return False, "expired"
        
        # Gate 2: 去重
        title_hash = item.content_hash
        simhash = item.simhash_value
        if not self.dedup.check_and_mark(title_hash, simhash, item.source, item.title):
            self._stats["rejected_duplicate"] += 1
            if self._collector:
                self._collector.record_received(item.to_dict(), passed=False, reason="duplicate")
            return False, "duplicate"
        
        # Gate 3: 相关性
        relevance_score = self.relevance.check_relevance(item)
        if relevance_score < 0.1:
            if not (item.is_geo_event() or item.urgency == NewsUrgency.IMMEDIATE):
                self._stats["rejected_irrelevant"] += 1
                if self._collector:
                    self._collector.record_received(item.to_dict(), passed=False, reason="irrelevant")
                return False, "irrelevant"
        
        # Gate 4: 重要性
        if item.urgency == NewsUrgency.LOW and item.raw_score < 0.15:
            self._stats["rejected_unimportant"] += 1
            if self._collector:
                self._collector.record_received(item.to_dict(), passed=False, reason="unimportant")
            return False, "unimportant"
        
        self._stats["passed"] += 1
        return True, "passed"
    
    def get_stats(self) -> Dict:
        return self._stats.copy()


class NewsPipelineV2:
    """
    统一新闻管线 V2
    
    数据流:
    Sources → Quality Gate → Fast/Deep Lane → Output
    """
    
    def __init__(self, sources: List[SourceAdapter] = None,
                 watchlist_codes: set = None,
                 watchlist_names: set = None,
                 dedup_db_path: str = None):
        self.sources = sources or []
        
        self.dedup = DedupEngineV2(db_path=dedup_db_path)
        self.relevance = RelevanceFilter(
            watchlist_codes=watchlist_codes or set(),
            watchlist_names=watchlist_names or set(),
        )
        self.quality_gate = QualityGate(self.dedup, self.relevance, collector=None)
        
        self.on_news_passed: Optional[Callable] = None
        self.on_geo_event: Optional[Callable] = None
        self.on_immediate: Optional[Callable] = None
        
        self._running = False
        self._collector = None
        self._stats = {
            "total_collected": 0,
            "total_passed": 0,
            "total_rejected": 0,
            "geo_events": 0,
            "immediate_news": 0,
            "pipeline_runs": 0,
            "last_run_time": 0,
            "last_run_duration_ms": 0,
        }
    
    def add_source(self, source: SourceAdapter):
        """添加新闻源"""
        self.sources.append(source)
        logger.info("[Pipeline] 添加新闻源: %s (%s)", source.source_name, source.source_id)
    
    def attach_collector(self, collector):
        """Attach data collector to quality gate"""
        self.quality_gate._collector = collector
        self._collector = collector

        # 地缘政治预筛器 (单例，避免每次创建)
        try:
            from app.factors.geo_pre_filter import GeoEventPreFilter
            self.geo_prefilter = GeoEventPreFilter()
            logger.info("[Pipeline] 地缘预筛器已初始化")
        except Exception as e:
            logger.warning("[Pipeline] 地缘预筛器初始化失败: %s", e)
            self.geo_prefilter = None
    
    async def run_once(self) -> List[UnifiedNewsItemV2]:
        """
        单次运行管线
        
        Returns:
            通过质量门禁的新闻列表
        """
        start = time.time()
        self._stats["pipeline_runs"] += 1
        
        # Step 1: 采集 (所有源并行)
        collected = []
        fetch_tasks = []
        for source in self.sources:
            if source.should_fetch():
                fetch_tasks.append(source.fetch_safe())
        
        if fetch_tasks:
            results = await asyncio.gather(*fetch_tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    logger.warning("[Pipeline] 源采集异常: %s", result)
                elif isinstance(result, list):
                    collected.extend(result)
        
        self._stats["total_collected"] += len(collected)
        
        # Step 2: 质量门禁
        passed = []
        rejected = []
        for item in collected:
            ok, reason = self.quality_gate.evaluate(item)
            if ok:
                passed.append(item)
            else:
                rejected.append((item, reason))
        
        self._stats["total_passed"] += len(passed)
        self._stats["total_rejected"] += len(rejected)
        
        # Step 3: 分类路由 + 地缘预筛
        for item in passed:
            # 地缘预筛 (零 LLM 消耗，纯正则)
            if self.geo_prefilter:
                geo_scan = self.geo_prefilter.scan(item.title, item.content[:500])
                item.geo_scan = geo_scan
                if geo_scan["is_geo_event"]:
                    logger.info(
                        "[Pipeline-Geo] 地缘标记 | severity=%s | paths=%s | %s",
                        geo_scan["severity"],
                        geo_scan["transmission_paths"],
                        item.title[:80]
                    )

            if item.is_geo_event():
                self._stats["geo_events"] += 1
                if self.on_geo_event:
                    self.on_geo_event(item)
            
            if item.is_immediate():
                self._stats["immediate_news"] += 1
                if self.on_immediate:
                    self.on_immediate(item)
            
            if self.on_news_passed:
                self.on_news_passed(item)
        
        # Step 4: 统计
        elapsed_ms = (time.time() - start) * 1000
        self._stats["last_run_time"] = time.time()
        self._stats["last_run_duration_ms"] = elapsed_ms
        
        if collected:
            logger.info(
                "[Pipeline] 采集=%d | 通过=%d | 拒绝=%d | 耗时=%.0fms",
                len(collected), len(passed), len(rejected), elapsed_ms
            )
        
        return passed
    
    def start(self, interval: int = 30):
        """启动持续运行"""
        self._running = True
        asyncio.create_task(self._run_loop(interval))
        logger.info("[Pipeline] 启动持续运行 (interval=%ds)", interval)
    
    def stop(self):
        """停止运行"""
        self._running = False
        logger.info("[Pipeline] 停止运行")
    
    async def _run_loop(self, interval: int):
        """持续运行循环"""
        while self._running:
            try:
                await self.run_once()
            except Exception as e:
                logger.error("[Pipeline] 运行异常: %s", e, exc_info=True)
            await asyncio.sleep(interval)
    
    def get_stats(self) -> Dict:
        return {
            **self._stats,
            "sources": [s.get_stats() for s in self.sources],
            "quality_gate": self.quality_gate.get_stats(),
            "dedup": self.dedup.get_stats(),
        }
