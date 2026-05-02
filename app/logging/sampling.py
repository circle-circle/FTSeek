"""
日志采样与降噪模块

功能：
1. RateLimitFilter — 同类型日志限速（如 "冷却中" 每 60 秒只输出 1 次）
2. SamplingFilter — 概率采样（高频 debug 日志按概率丢弃）
3. DedupFilter — 短时去重（相同内容 N 秒内只记一次）

使用方式（在 logging_config.py 中集成）：
    from app.logging.sampling import RateLimitFilter, SamplingFilter
    logger.addFilter(RateLimitFilter("cooldown", interval=60))
    logger.addFilter(SamplingFilter("mtf", probability=0.1))
"""

import logging
import threading
import time
from typing import Dict, Optional


class RateLimitFilter(logging.Filter):
    """
    按消息关键词限速过滤器。

    对包含指定关键词的日志，在 interval 秒内只放行 limit 条。
    超出限制后降级为 DEBUG（或直接丢弃）。

    Args:
        keywords: 匹配的关键词列表或字符串
        interval: 限速窗口（秒），默认 60
        limit: 窗口内最大放行条数，默认 1
        downgrade_level: 超出限制后降级的级别，默认 DEBUG
    """

    def __init__(
        self,
        keywords,
        interval: float = 60.0,
        limit: int = 1,
        downgrade_level: int = logging.DEBUG,
    ):
        super().__init__()
        self.keywords = [keywords] if isinstance(keywords, str) else list(keywords)
        self.interval = interval
        self.limit = limit
        self.downgrade_level = downgrade_level
        self._lock = threading.Lock()
        # key -> (count, window_start)
        self._windows: Dict[str, tuple] = {}

    def _match(self, msg: str) -> Optional[str]:
        for kw in self.keywords:
            if kw in msg:
                return kw
        return None

    def filter(self, record: logging.LogRecord) -> bool:
        kw = self._match(record.getMessage())
        if kw is None:
            return True  # 不匹配，正常放行

        now = time.time()
        with self._lock:
            count, window_start = self._windows.get(kw, (0, now))
            if now - window_start >= self.interval:
                # 新窗口
                self._windows[kw] = (1, now)
                return True
            if count < self.limit:
                self._windows[kw] = (count + 1, window_start)
                return True
            # 超出限制：降级级别，让下游 handler 决定是否记录
            record.levelno = self.downgrade_level
            record.levelname = logging.getLevelName(self.downgrade_level)
            # 如果原始级别高于降级级别，则丢弃；否则保留
            return record.levelno >= logging.DEBUG


class SamplingFilter(logging.Filter):
    """
    概率采样过滤器 — 高频日志按概率丢弃。

    对包含指定关键词的日志，只保留 probability 比例。
    适用于 K 线级别 debug 日志、趋势同步日志等。

    Args:
        keywords: 匹配关键词
        probability: 保留概率 (0.0-1.0)，默认 0.1（10%）
    """

    def __init__(self, keywords, probability: float = 0.1):
        super().__init__()
        self.keywords = [keywords] if isinstance(keywords, str) else list(keywords)
        self.probability = max(0.0, min(1.0, probability))
        self._lock = threading.Lock()
        self._counter: Dict[str, int] = {}
        self._sample_step: Dict[str, int] = {}

    def _match(self, msg: str) -> Optional[str]:
        for kw in self.keywords:
            if kw in msg:
                return kw
        return None

    def filter(self, record: logging.LogRecord) -> bool:
        kw = self._match(record.getMessage())
        if kw is None:
            return True

        with self._lock:
            step = self._sample_step.get(kw, 0)
            if step == 0:
                # 根据概率计算采样步长（确定性采样，避免随机数开销）
                if self.probability >= 1.0:
                    step = 1
                elif self.probability <= 0.0:
                    step = 10**9
                else:
                    step = max(1, int(1.0 / self.probability))
                self._sample_step[kw] = step

            count = self._counter.get(kw, 0) + 1
            self._counter[kw] = count
            return count % step == 1  # 每 step 条保留第 1 条


class DedupFilter(logging.Filter):
    """
    短时去重过滤器 — 相同内容 N 秒内只记一次。

    适用于波动较小的重复日志（如 "追踪止盈更新"、"止损检查通过"）。

    Args:
        interval: 去重窗口（秒），默认 300
    """

    def __init__(self, interval: float = 300.0):
        super().__init__()
        self.interval = interval
        self._lock = threading.Lock()
        # (message_hash, levelno) -> last_logged_time
        self._last_seen: Dict[tuple, float] = {}

    def filter(self, record: logging.LogRecord) -> bool:
        key = (record.getMessage(), record.levelno)
        now = time.time()
        with self._lock:
            last = self._last_seen.get(key, 0)
            if now - last >= self.interval:
                self._last_seen[key] = now
                return True
            return False
