"""
信号去重器 — 防止短时间内重复信号导致重复下单

Task #1 (2026-06-17): 安全基础设施
- 60秒窗口内, 同 symbol+direction+strategy_id 的信号视为重复
- 定期清理过期条目, 防止内存泄漏
"""
import logging
import threading
import time
from typing import Dict, Optional

from app.utils.timezone import now_et

logger = logging.getLogger(__name__)


class SignalDeduplicator:
    """
    信号去重器 (单例)

    - DEDUP_WINDOW_SECONDS = 60
    - key = symbol + direction + strategy_id
    - 60秒窗口内同key信号视为重复, 拒绝处理
    """

    _instance: Optional["SignalDeduplicator"] = None
    _lock = threading.Lock()

    DEDUP_WINDOW_SECONDS = 60  # 去重窗口

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._state_lock = threading.Lock()
        # key -> last_seen_timestamp (monotonic)
        self._seen: Dict[str, float] = {}
        self._cleanup_counter = 0
        logger.info(
            f"[SignalDeduplicator] 初始化完成 | window={self.DEDUP_WINDOW_SECONDS}s"
        )

    def _make_key(self, symbol: str, direction: str, strategy_id: str = "") -> str:
        """构建去重key"""
        return f"{symbol}|{direction.upper()}|{strategy_id}"

    def is_duplicate(self, symbol: str, direction: str, strategy_id: str = "") -> bool:
        """
        检查信号是否重复

        Args:
            symbol: 标的代码
            direction: 方向 (LONG/SHORT)
            strategy_id: 策略ID (可选)

        Returns:
            True = 重复信号, 应跳过
            False = 新信号, 可处理
        """
        key = self._make_key(symbol, direction, strategy_id)
        now = time.monotonic()

        with self._state_lock:
            # 定期清理过期条目 (每 50 次检查清理一次)
            self._cleanup_counter += 1
            if self._cleanup_counter >= 50:
                self._cleanup(now)
                self._cleanup_counter = 0

            last_seen = self._seen.get(key)
            if last_seen is not None:
                elapsed = now - last_seen
                if elapsed < self.DEDUP_WINDOW_SECONDS:
                    logger.info(
                        f"[SignalDedup] 重复信号被过滤: {symbol} {direction} "
                        f"strategy={strategy_id} | 距上次{elapsed:.1f}s < {self.DEDUP_WINDOW_SECONDS}s"
                    )
                    return True

            # 记录本次信号
            self._seen[key] = now
            return False

    def _cleanup(self, now: float) -> None:
        """清理过期条目"""
        expired_keys = [
            k for k, ts in self._seen.items()
            if (now - ts) > self.DEDUP_WINDOW_SECONDS * 2
        ]
        for k in expired_keys:
            del self._seen[k]
        if expired_keys:
            logger.debug(f"[SignalDedup] 清理 {len(expired_keys)} 条过期记录")


# ── 全局单例访问 ──
def get_signal_deduplicator() -> SignalDeduplicator:
    """获取 SignalDeduplicator 全局单例"""
    return SignalDeduplicator()
