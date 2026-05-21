"""
CacheManager — 统一缓存管理器

职责:
- 管理所有共享缓存（trailing_stop, peak_price, news_sentiment, flash_snapshots 等）
- 提供统一的 get/set/cleanup 接口
- 支持 TTL 过期清理

从 data_subscriber.py 提取，降低耦合，提升可测试性
"""
import logging
import threading
import time
from typing import Dict, Optional, Tuple

logger = logging.getLogger("ftseek.cache_manager")


class CacheManager:
    """统一缓存管理器"""

    _instance: Optional['CacheManager'] = None
    _lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> 'CacheManager':
        """单例获取"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def __init__(self):
        """初始化所有缓存"""
        # 追踪止盈缓存：code -> 追踪止损价
        self._trailing_stop_cache: Dict[str, float] = {}
        # 追踪止盈触发时间：code -> timestamp
        self._trailing_exit_time: Dict[str, float] = {}
        # 持仓峰值价格：code -> peak_price
        self._peak_price_cache: Dict[str, float] = {}
        # 新闻情绪缓存：code -> (score, timestamp)
        self._news_sentiment_cache: Dict[str, Tuple[float, float]] = {}
        self._news_cache_ttl = 1800  # 30 分钟
        # Flash Exit 快照：code -> (price, timestamp)
        self._flash_snapshots: Dict[str, Tuple[float, float]] = {}
        # 推送冷却：code -> last_push_time
        self._cooldown: Dict[str, float] = {}
        self._cooldown_lock = threading.Lock()
        # 止损冷却：code -> last_stop_loss_time
        self._stop_loss_cooldown: Dict[str, float] = {}
        # 止损失败计数：code -> int
        self._stop_loss_fail_count: Dict[str, int] = {}
        # 软止损预警冷却：code -> last_warning_timestamp
        self.soft_stop_warnings: Dict[str, float] = {}
        # 开仓原子锁：code -> last open timestamp
        self._open_position_ts: Dict[str, float] = {}
        self._open_lock = threading.Lock()

        logger.info("[CacheManager] 初始化完成")

    # ============================================================
    # Trailing Stop 相关
    # ============================================================

    def get_trailing_stop(self, code: str) -> float:
        """获取追踪止损价"""
        return self._trailing_stop_cache.get(code, 0.0)

    def set_trailing_stop(self, code: str, price: float) -> None:
        """设置追踪止损价"""
        self._trailing_stop_cache[code] = price

    def get_trailing_exit_time(self, code: str) -> float:
        """获取追踪止盈触发时间"""
        return self._trailing_exit_time.get(code, 0.0)

    def set_trailing_exit_time(self, code: str, timestamp: float) -> None:
        """设置追踪止盈触发时间"""
        self._trailing_exit_time[code] = timestamp

    def clear_trailing_stop(self, code: str) -> None:
        """清除追踪止损相关缓存"""
        self._trailing_stop_cache.pop(code, None)
        self._trailing_exit_time.pop(code, None)

    # ============================================================
    # Peak Price 相关
    # ============================================================

    def get_peak_price(self, code: str) -> float:
        """获取峰值价格"""
        return self._peak_price_cache.get(code, 0.0)

    def set_peak_price(self, code: str, price: float) -> None:
        """设置峰值价格"""
        self._peak_price_cache[code] = price

    def update_peak_price(self, code: str, current_price: float) -> bool:
        """更新峰值价格（如果当前价格更高）
        
        Returns:
            True if updated, False otherwise
        """
        current_peak = self._peak_price_cache.get(code, 0.0)
        if current_price > current_peak:
            self._peak_price_cache[code] = current_price
            return True
        return False

    def clear_peak_price(self, code: str) -> None:
        """清除峰值价格"""
        self._peak_price_cache.pop(code, None)

    # ============================================================
    # News Sentiment 相关
    # ============================================================

    def get_news_sentiment(self, code: str) -> Tuple[float, float]:
        """获取新闻情绪缓存
        
        Returns:
            (score, timestamp) or (0.0, 0.0) if not cached
        """
        return self._news_sentiment_cache.get(code, (0.0, 0.0))

    def set_news_sentiment(self, code: str, score: float) -> None:
        """设置新闻情绪缓存"""
        self._news_sentiment_cache[code] = (score, time.time())

    def is_news_sentiment_expired(self, code: str) -> bool:
        """检查新闻情绪是否过期"""
        if code not in self._news_sentiment_cache:
            return True
        _, cached_time = self._news_sentiment_cache[code]
        return time.time() - cached_time > self._news_cache_ttl

    # ============================================================
    # Flash Exit 相关
    # ============================================================

    def get_flash_snapshot(self, code: str) -> Optional[Tuple[float, float]]:
        """获取 Flash Exit 快照"""
        return self._flash_snapshots.get(code)

    def set_flash_snapshot(self, code: str, price: float, timestamp: float) -> None:
        """设置 Flash Exit 快照"""
        self._flash_snapshots[code] = (price, timestamp)

    # ============================================================
    # Cooldown 相关
    # ============================================================

    def get_cooldown_time(self, code: str) -> float:
        """获取推送冷却时间"""
        with self._cooldown_lock:
            return self._cooldown.get(code, 0.0)

    def set_cooldown_time(self, code: str, timestamp: float) -> None:
        """设置推送冷却时间"""
        with self._cooldown_lock:
            self._cooldown[code] = timestamp

    def is_cooled_down(self, code: str, cooldown_seconds: int) -> bool:
        """检查是否在冷却期内"""
        with self._cooldown_lock:
            last_push = self._cooldown.get(code, 0.0)
            return (time.time() - last_push) < cooldown_seconds

    def cleanup_cooldown(self, code: str) -> None:
        """清理冷却记录"""
        with self._cooldown_lock:
            self._cooldown.pop(code, None)

    # ============================================================
    # Stop Loss Cooldown 相关
    # ============================================================

    def get_stop_loss_cooldown(self, code: str) -> float:
        """获取止损冷却时间"""
        return self._stop_loss_cooldown.get(code, 0.0)

    def set_stop_loss_cooldown(self, code: str, timestamp: float) -> None:
        """设置止损冷却时间"""
        self._stop_loss_cooldown[code] = timestamp

    def get_stop_loss_fail_count(self, code: str) -> int:
        """获取止损失败计数"""
        return self._stop_loss_fail_count.get(code, 0)

    def increment_stop_loss_fail(self, code: str) -> int:
        """增加止损失败计数"""
        count = self._stop_loss_fail_count.get(code, 0) + 1
        self._stop_loss_fail_count[code] = count
        return count

    def reset_stop_loss_fail(self, code: str) -> None:
        """重置止损失败计数"""
        self._stop_loss_fail_count.pop(code, None)

    # ============================================================
    # Soft Stop Warning 相关
    # ============================================================

    def get_soft_stop_warning(self, code: str) -> float:
        """获取软止损预警时间"""
        return self.soft_stop_warnings.get(code, 0.0)

    def set_soft_stop_warning(self, code: str, timestamp: float) -> None:
        """设置软止损预警时间"""
        self.soft_stop_warnings[code] = timestamp

    # ============================================================
    # Open Position Lock 相关
    # ============================================================

    def get_open_position_ts(self, code: str) -> float:
        """获取开仓时间戳"""
        with self._open_lock:
            return self._open_position_ts.get(code, 0.0)

    def set_open_position_ts(self, code: str, timestamp: float) -> None:
        """设置开仓时间戳"""
        with self._open_lock:
            self._open_position_ts[code] = timestamp

    # ============================================================
    # 全局清理
    # ============================================================

    def cleanup_all(self, code: str) -> None:
        """清理指定标的的所有缓存"""
        self.clear_trailing_stop(code)
        self.clear_peak_price(code)
        self._news_sentiment_cache.pop(code, None)
        self._flash_snapshots.pop(code, None)
        self.cleanup_cooldown(code)
        self._stop_loss_cooldown.pop(code, None)
        self.reset_stop_loss_fail(code)
        self.soft_stop_warnings.pop(code, None)
        with self._open_lock:
            self._open_position_ts.pop(code, None)
        logger.info(f"[CacheManager] 已清理 {code} 的所有缓存")

    def cleanup_expired(self) -> int:
        """清理所有过期缓存
        
        Returns:
            清理的条目数
        """
        cleaned = 0
        now = time.time()

        # 清理过期的新闻情绪
        expired_codes = [
            code for code, (_, ts) in self._news_sentiment_cache.items()
            if now - ts > self._news_cache_ttl
        ]
        for code in expired_codes:
            self._news_sentiment_cache.pop(code, None)
            cleaned += 1

        # 清理过期的冷却记录（超过 24 小时）
        with self._cooldown_lock:
            expired_cooldowns = [
                code for code, ts in self._cooldown.items()
                if now - ts > 86400
            ]
            for code in expired_cooldowns:
                self._cooldown.pop(code, None)
                cleaned += 1

        if cleaned > 0:
            logger.debug(f"[CacheManager] 清理了 {cleaned} 条过期缓存")
        return cleaned


# 全局单例
cache_manager = CacheManager.get_instance()
