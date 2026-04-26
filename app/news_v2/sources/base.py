"""
FTSeek 新闻源适配器基类

设计原则:
1. 所有数据源统一实现 SourceAdapter 接口
2. 失败隔离: 单个源失败不影响其他源
3. 健康监控: 连续失败自动暂停
4. 可测试: 每个源可独立测试
"""

import logging
import time
from abc import ABC, abstractmethod
from typing import List, Dict, Optional
from app.news_v2.models import UnifiedNewsItemV2

logger = logging.getLogger("ftseek.news_v2.sources")


class SourceHealthMonitor:
    """源健康监控 — 连续失败自动暂停"""
    
    def __init__(self, max_consecutive_failures: int = 5, pause_minutes: int = 30):
        self.max_consecutive_failures = max_consecutive_failures
        self.pause_minutes = pause_minutes
        
        self._consecutive_failures = 0
        self._pause_until = 0.0
        self._total_calls = 0
        self._total_success = 0
    
    def record_success(self):
        self._consecutive_failures = 0
        self._total_calls += 1
        self._total_success += 1
    
    def record_failure(self, error: str = ""):
        self._consecutive_failures += 1
        self._total_calls += 1
        if self._consecutive_failures >= self.max_consecutive_failures:
            self._pause_until = time.time() + self.pause_minutes * 60
            logger.warning(
                "源暂停 %d 分钟 (连续失败 %d 次): %s",
                self.pause_minutes, self._consecutive_failures, error
            )
    
    def is_paused(self) -> bool:
        if time.time() < self._pause_until:
            return True
        if self._pause_until > 0:
            # 暂停时间已过，重置
            self._pause_until = 0.0
            self._consecutive_failures = 0
            logger.info("源暂停结束，恢复正常采集")
        return False
    
    @property
    def success_rate(self) -> float:
        if self._total_calls == 0:
            return 1.0
        return self._total_success / self._total_calls
    
    def get_status(self) -> Dict:
        remaining_pause = max(0, self._pause_until - time.time())
        return {
            "paused": self.is_paused(),
            "pause_remaining_seconds": remaining_pause,
            "consecutive_failures": self._consecutive_failures,
            "success_rate": self.success_rate,
            "total_calls": self._total_calls,
        }


class SourceAdapter(ABC):
    """
    新闻源适配器基类
    
    所有数据源必须实现:
    - fetch() → List[UnifiedNewsItemV2]
    - source_id → str (唯一标识)
    """
    
    def __init__(self, interval_seconds: int = 60):
        self.interval_seconds = interval_seconds
        self.health = SourceHealthMonitor()
        self._last_fetch_time = 0.0
        self._last_fetch_count = 0
    
    @property
    @abstractmethod
    def source_id(self) -> str:
        """源唯一标识 (如: "rsshub_wsj")"""
        pass
    
    @property
    @abstractmethod
    def source_name(self) -> str:
        """人类可读名称 (如: "华尔街日报")"""
        pass
    
    @abstractmethod
    async def fetch(self) -> List[UnifiedNewsItemV2]:
        """
        采集新闻
        
        Returns:
            新闻列表 (统一格式)
        """
        pass
    
    async def fetch_safe(self) -> List[UnifiedNewsItemV2]:
        """
        安全采集 (自动健康监控)
        """
        if self.health.is_paused():
            return []
        
        try:
            items = await self.fetch()
            self.health.record_success()
            self._last_fetch_time = time.time()
            self._last_fetch_count = len(items)
            return items
        except Exception as e:
            self.health.record_failure(str(e))
            logger.warning("[%s] 采集失败: %s", self.source_name, e)
            return []
    
    def should_fetch(self) -> bool:
        """是否应该采集 (基于间隔时间)"""
        return (time.time() - self._last_fetch_time) >= self.interval_seconds
    
    def get_stats(self) -> Dict:
        return {
            "source_id": self.source_id,
            "source_name": self.source_name,
            "interval_seconds": self.interval_seconds,
            "last_fetch_time": self._last_fetch_time,
            "last_fetch_count": self._last_fetch_count,
            "health": self.health.get_status(),
        }
