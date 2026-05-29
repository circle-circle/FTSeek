"""符号+方向级互斥锁 - 确保同一标的同方向不被多个策略同时持有

Task #50 (2026-05-28): Main/Scalping 策略互斥锁

与现有 ``app/coordinator/symbol_lock.py`` (symbol 级) 的区别:
- 本模块按 (symbol, direction) 两元组锁定, 允许对冲场景
  (同标的 Main LONG + Scalping SHORT 不冲突)
- 由 ``signal_pipeline``/``order_executor``/``scalping.engine`` 在
  入场前调用 ``try_acquire``, 平仓时调用 ``release``
- 单例 + 线程安全 (RLock)
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class SymbolLockManager:
    """符号+方向级锁管理器 (单例)

    确保同一标的同一方向不被两个策略同时持有。
    不同方向 (对冲) 允许。
    """

    _instance: Optional["SymbolLockManager"] = None
    _init_lock = threading.Lock()

    def __new__(cls):
        with cls._init_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return
        # key: (symbol, direction) -> (owner_strategy, lock_time)
        self._locks: Dict[Tuple[str, str], Tuple[str, datetime]] = {}
        self._lock = threading.RLock()
        # 冲突历史 (保留最近 N 条)
        self._conflict_history: List[Dict] = []
        self._max_conflict_history = 500
        self._initialized = True
        logger.info("[SymbolLock] 初始化完成 (symbol+direction 级别)")

    # ──────────────────────────────────────────────
    # 公开 API
    # ──────────────────────────────────────────────
    def try_acquire(self, symbol: str, direction: str, owner: str) -> bool:
        """尝试获取符号+方向级锁

        Args:
            symbol:    股票代码 (如 "AAPL")
            direction: 方向 ("LONG" 或 "SHORT")
            owner:     策略所有者 ("main" 或 "scalping" 等)

        Returns:
            True  获取成功 (或同策略幂等重入)
            False 被拒 (已被其他策略占有)
        """
        key = self._key(symbol, direction)
        if key is None:
            # 非法入参不参与互斥, 直接放行
            return True
        with self._lock:
            existing = self._locks.get(key)
            if existing is not None:
                existing_owner, lock_time = existing
                if existing_owner == owner:
                    # 同一策略重复获取 -> 幂等放行
                    return True
                logger.warning(
                    "[SymbolLock] 互斥拒绝: %s %s 已被 %s 持有(自%s), %s 入场被拒",
                    symbol, direction, existing_owner,
                    lock_time.strftime("%H:%M:%S"), owner,
                )
                self._record_conflict(symbol, direction, existing_owner, owner)
                return False

            self._locks[key] = (owner, datetime.now())
            logger.debug("[SymbolLock] %s %s 由 %s 获取", symbol, direction, owner)
            return True

    def release(self, symbol: str, direction: str, owner: str) -> bool:
        """释放符号+方向级锁 (仅 owner 可释放)

        Returns:
            True  释放成功或已释放 (幂等)
            False 释放失败 (持有者不是 owner)
        """
        key = self._key(symbol, direction)
        if key is None:
            return True
        with self._lock:
            existing = self._locks.get(key)
            if existing is None:
                return True  # 幂等
            existing_owner, _ = existing
            if existing_owner != owner:
                logger.warning(
                    "[SymbolLock] 释放权限不匹配: %s %s holder=%s, tried=%s",
                    symbol, direction, existing_owner, owner,
                )
                return False
            del self._locks[key]
            logger.debug("[SymbolLock] %s %s 由 %s 释放", symbol, direction, owner)
            return True

    def is_locked(self, symbol: str, direction: str) -> Optional[str]:
        """查询符号+方向是否被锁定

        Returns:
            锁定的策略名称, 或 None (未锁定)
        """
        key = self._key(symbol, direction)
        if key is None:
            return None
        with self._lock:
            existing = self._locks.get(key)
            return existing[0] if existing else None

    def get_all_locks(self) -> Dict[Tuple[str, str], Tuple[str, datetime]]:
        """获取所有活跃锁 (调试用)"""
        with self._lock:
            return dict(self._locks)

    def release_all_by_owner(self, owner: str) -> int:
        """释放某策略的所有锁 (策略重启时使用)"""
        count = 0
        with self._lock:
            keys_to_remove = [k for k, (o, _) in self._locks.items() if o == owner]
            for key in keys_to_remove:
                del self._locks[key]
                count += 1
        if count:
            logger.info("[SymbolLock] 释放 %s 的 %d 个锁", owner, count)
        return count

    def get_conflict_history(self, limit: int = 50) -> List[Dict]:
        """获取最近的冲突历史"""
        with self._lock:
            return list(self._conflict_history[-limit:])

    def reset(self) -> None:
        """清空所有锁 (单测/重启使用)"""
        with self._lock:
            self._locks.clear()
            self._conflict_history.clear()

    # ──────────────────────────────────────────────
    # 内部工具
    # ──────────────────────────────────────────────
    @staticmethod
    def _key(symbol: str, direction: str) -> Optional[Tuple[str, str]]:
        if not symbol or not direction:
            return None
        try:
            return (str(symbol).upper(), str(direction).upper())
        except Exception:
            return None

    def _record_conflict(self, symbol: str, direction: str,
                         holder: str, rejected: str) -> None:
        self._conflict_history.append({
            "time": datetime.now(),
            "symbol": symbol,
            "direction": direction,
            "holder": holder,
            "rejected": rejected,
        })
        if len(self._conflict_history) > self._max_conflict_history:
            self._conflict_history = self._conflict_history[-self._max_conflict_history:]


def get_symbol_lock_manager() -> SymbolLockManager:
    """获取全局 SymbolLockManager 单例"""
    return SymbolLockManager()
