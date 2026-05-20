"""
SymbolLockManager — 同 symbol 同时只能被 1 个 strategy 持仓 (Phase A 2026-05-14)

观察模式 (observe_only=True): 只记录冲突, 不阻断
强制模式: 已锁的 symbol 拒绝其他 strategy 持有
"""
from __future__ import annotations
import logging
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class _LockEvent:
    timestamp: float
    symbol: str
    requester: str
    holder: Optional[str]
    decision: str  # ACQUIRED | DENIED | REENTRANT | RELEASED


class SymbolLockManager:
    """同 symbol 互斥锁 (线程安全)"""

    def __init__(self, observe_only: bool = True):
        self._locks: Dict[str, Tuple[str, float]] = {}  # symbol -> (strategy, ts)
        self._lock = threading.Lock()
        self._observe_only = observe_only
        self._events: List[_LockEvent] = []
        self._max_event_history = 500

    def set_observe_only(self, observe: bool):
        with self._lock:
            self._observe_only = observe
        logger.info(f"[SymbolLockManager] observe_only={observe}")

    def is_observe_only(self) -> bool:
        """B.6 (2026-05-14): 公开访问"""
        return self._observe_only

    def acquire(self, symbol: str, strategy: str) -> bool:
        """
        请求 symbol 锁.
        Returns:
            True: 拿到 (或同 strategy 重入); observe 模式下即使被占用也 True (告警)
            False: 强制模式下被占用
        """
        with self._lock:
            existing = self._locks.get(symbol)
            if existing is None:
                self._locks[symbol] = (strategy, time.time())
                self._record(symbol, strategy, None, "ACQUIRED")
                return True
            holder, _ = existing
            if holder == strategy:
                # 同 strategy 重入 OK
                self._record(symbol, strategy, holder, "REENTRANT")
                return True
            # 冲突
            decision = "OBSERVE_CONFLICT" if self._observe_only else "DENIED"
            self._record(symbol, strategy, holder, decision)
            if self._observe_only:
                logger.warning(
                    f"[SymbolLock·OBSERVE] {symbol} held by {holder}, requester={strategy} — 放行"
                )
                # observe 模式不改变 holder, 但放行 (后续 PositionRegistry 会出现 2 个仓位 → reconcile 时告警)
                return True
            logger.warning(
                f"[SymbolLock·ENFORCE] {symbol} held by {holder}, {strategy} REJECTED"
            )
            return False

    def release(self, symbol: str, strategy: Optional[str] = None):
        """
        释放锁. 如指定 strategy, 仅当 holder 匹配才释放 (防止误释放).
        """
        with self._lock:
            existing = self._locks.get(symbol)
            if existing is None:
                return
            holder, _ = existing
            if strategy is not None and holder != strategy:
                logger.warning(
                    f"[SymbolLock] release({symbol}) by {strategy} 但 holder={holder}, 忽略"
                )
                return
            del self._locks[symbol]
            self._record(symbol, strategy or holder, holder, "RELEASED")

    def owner(self, symbol: str) -> Optional[str]:
        with self._lock:
            existing = self._locks.get(symbol)
            return existing[0] if existing else None

    def all_locked(self) -> Dict[str, str]:
        """{symbol: strategy} 当前持有的锁"""
        with self._lock:
            return {s: v[0] for s, v in self._locks.items()}

    def reconcile_with_positions(self, positions_by_strategy: Dict[str, List[str]]):
        """
        与真实持仓对账: positions_by_strategy = {strategy: [symbols]}
        - 锁里有但持仓没有 → release
        - 持仓有但锁里没有 → 补 acquire
        """
        with self._lock:
            actual_owners: Dict[str, str] = {}
            for strat, symbols in positions_by_strategy.items():
                for sym in symbols:
                    if sym in actual_owners and actual_owners[sym] != strat:
                        logger.warning(
                            f"[SymbolLock·RECONCILE] {sym} 持仓存在多 owner: "
                            f"{actual_owners[sym]} vs {strat}"
                        )
                    actual_owners[sym] = strat

            # 锁里有但持仓没有 → release
            for sym in list(self._locks.keys()):
                if sym not in actual_owners:
                    holder = self._locks[sym][0]
                    del self._locks[sym]
                    self._record(sym, holder, holder, "RECONCILE_RELEASE")

            # 持仓有但锁里没有 → acquire
            for sym, strat in actual_owners.items():
                if sym not in self._locks:
                    self._locks[sym] = (strat, time.time())
                    self._record(sym, strat, None, "RECONCILE_ACQUIRE")

    def get_recent_events(self, n: int = 50) -> List[Dict]:
        with self._lock:
            return [
                {
                    "ts": e.timestamp,
                    "symbol": e.symbol,
                    "requester": e.requester,
                    "holder": e.holder,
                    "decision": e.decision,
                }
                for e in self._events[-n:]
            ]

    def _record(self, symbol: str, requester: str, holder: Optional[str], decision: str):
        e = _LockEvent(time.time(), symbol, requester, holder, decision)
        self._events.append(e)
        if len(self._events) > self._max_event_history:
            self._events = self._events[-self._max_event_history:]


_singleton: Optional[SymbolLockManager] = None


def get_symbol_lock_manager(observe_only: bool = True) -> SymbolLockManager:
    global _singleton
    if _singleton is None:
        _singleton = SymbolLockManager(observe_only=observe_only)
    return _singleton
