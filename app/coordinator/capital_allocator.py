"""
CapitalAllocator — 实时资金账本 (Phase A 2026-05-14)

强制每个 strategy 的 capital_pct 上限. 取代当前"软约束"。

观察模式 (observe_only=True): 记录所有 reserve/release, 但永远 return True
强制模式 (observe_only=False): 超限直接拒绝
"""
from __future__ import annotations
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class CapitalSnapshot:
    """供 dashboard / daily_review 读"""
    timestamp: float
    by_strategy: Dict[str, float]
    total_reserved: float
    max_total: float
    headroom: float  # max_total - total_reserved


@dataclass
class _ReserveEvent:
    timestamp: float
    strategy: str
    delta: float           # 正数=reserve, 负数=release
    decision: str          # ALLOWED / BLOCKED_STRATEGY_CAP / BLOCKED_TOTAL_CAP
    snapshot_after: float  # strategy 当前总占用


class CapitalAllocator:
    """资金账本 + 上限强制 (线程安全)"""

    def __init__(
        self,
        caps: Dict[str, float],
        max_total_pct: float = 0.90,
        observe_only: bool = True,
        bucket_map: Optional[Dict[str, str]] = None,
        db_path: Optional[str] = None,
    ):
        """
        Args:
            caps: {bucket_name: max_pct}, 例 {"main": 0.65, "scalping": 0.25}
            max_total_pct: 全局上限 (所有 bucket 加总)
            observe_only: True=观察模式 (Phase A 默认), False=强制
            bucket_map: {strategy_name: bucket_name} — 多策略共享 bucket 时用
                        例 {"fast_lane": "main"} 让 fast_lane 占用计入 main 桶
                        缺省 → strategy_name = bucket_name
        """
        # P1-7: stash db_path for _init_persist_db
        self._persist_init_db_path = db_path
        self._caps = dict(caps)
        self._max_total = max_total_pct
        self._observe_only = observe_only
        self._bucket_map = dict(bucket_map or {})
        # _allocations key 是 bucket, 不是 strategy
        self._allocations: Dict[str, float] = {s: 0.0 for s in caps}
        # _by_strategy 仅供查看/报表 — 同 bucket 内多策略各自累加
        self._by_strategy: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._events: List[_ReserveEvent] = []
        self._max_event_history = 500
        # P1-7 (2026-06-12): persistent ledger
        self._db_path: Optional[str] = None
        self._db_lock = threading.Lock()
        if self._persist_init_db_path is not None:
            try:
                self._init_persist_db(self._persist_init_db_path)
                self._load_persist_state()
                logger.info(
                    f"[CapitalAllocator] persist enabled db={self._db_path} "
                    f"restored allocations={self._allocations} by_strategy={self._by_strategy}"
                )
            except Exception as _e:
                logger.error(f"[CapitalAllocator] persist init failed (fallback to memory): {_e}")
                self._db_path = None

    def _bucket_for(self, strategy: str) -> str:
        return self._bucket_map.get(strategy, strategy)

    def set_observe_only(self, observe: bool):
        """运行时切换 (灰度)"""
        with self._lock:
            self._observe_only = observe
        logger.info(f"[CapitalAllocator] observe_only={observe}")

    def try_reserve(self, strategy: str, position_pct: float) -> bool:
        """尝试预留资金额度。

        ⚠️ 当前限制:
        - observe_only=True 模式下永远返回 True (仅记录不阻断)
        - 主订单路径 (order_executor._submit_order) 尚未在下单前调用此方法
        - signal.position_pct 默认为 0.0, 导致预留金额为 0
        以上问题计划在 Phase B 修复, 届时将:
        1. _submit_order 下单前调用 try_reserve
        2. 从 _get_order_qty 结果计算 position_pct
        3. observe_only 切为 False

        资金记账走 bucket (B.3 2026-05-14): 多策略共享 bucket 时, 占用计入同一桶.
        例 strategy=fast_lane, bucket_map={"fast_lane":"main"} → 实际记到 main 桶, 受 main 的 cap 限制.

        Returns:
            True: 预留成功 (observe 模式总是 True, 但 BLOCKED 事件仍记录)
            False: 强制模式下被拒
        """
        with self._lock:
            bucket = self._bucket_for(strategy)
            current = self._allocations.get(bucket, 0.0)
            cap = self._caps.get(bucket, 0.0)
            total = sum(self._allocations.values())

            decision = "ALLOWED"
            if cap > 0 and current + position_pct > cap:
                decision = "BLOCKED_STRATEGY_CAP"
            elif total + position_pct > self._max_total:
                decision = "BLOCKED_TOTAL_CAP"

            allowed = (decision == "ALLOWED")
            if allowed:
                # H7 (2026-06-03): 只有 allowed 才记账, 避免 BLOCKED 事件累加导致账本失真
                self._allocations[bucket] = current + position_pct
                self._by_strategy[strategy] = self._by_strategy.get(strategy, 0.0) + position_pct
                snapshot_after = self._allocations[bucket]
                self._record_event(strategy, position_pct, decision, snapshot_after)
                self._persist_state(bucket, strategy)  # P1-7
                return True

            # BLOCKED: 只记录事件, 不累加账本
            self._record_event(strategy, position_pct, decision, current)

            if self._observe_only:
                logger.warning(
                    f"[CapitalAllocator·OBSERVE] {strategy}->{bucket} reserve {position_pct:.3f} "
                    f"would have been {decision} (current={current:.3f}, cap={cap:.3f}, "
                    f"total={total:.3f}, max_total={self._max_total:.3f}) — 放行但不记账"
                )
                return True  # observe 模式放行, 但不累加账本

            # 强制模式 + 拒绝
            logger.warning(
                f"[CapitalAllocator·ENFORCE] {strategy}->{bucket} reserve {position_pct:.3f} REJECTED: "
                f"{decision} (current={current:.3f}, cap={cap:.3f})"
            )
            return False

    def release(self, strategy: str, position_pct: float):
        """释放 strategy 的预留资金 (平仓时调用) — 走 bucket"""
        with self._lock:
            bucket = self._bucket_for(strategy)
            current = self._allocations.get(bucket, 0.0)
            new_val = max(0.0, current - position_pct)
            self._allocations[bucket] = new_val
            self._by_strategy[strategy] = max(0.0, self._by_strategy.get(strategy, 0.0) - position_pct)
            self._record_event(strategy, -position_pct, "RELEASED", new_val)
            self._persist_state(bucket, strategy)  # P1-7

    def force_set(self, strategy: str, value: float):
        """从持仓快照同步 (启动恢复 / 定期 reconcile 用) — 走 bucket"""
        with self._lock:
            bucket = self._bucket_for(strategy)
            self._allocations[bucket] = max(0.0, value)
            self._record_event(strategy, 0.0, "FORCE_SET", value)
            self._persist_state(bucket, strategy)  # P1-7

    def snapshot(self) -> CapitalSnapshot:
        """当前账本快照. by_strategy 字段是 bucket 视图 (向后兼容现有调用方)."""
        with self._lock:
            by_s = dict(self._allocations)
            total = sum(by_s.values())
            return CapitalSnapshot(
                timestamp=time.time(),
                by_strategy=by_s,
                total_reserved=total,
                max_total=self._max_total,
                headroom=self._max_total - total,
            )

    def by_strategy_snapshot(self) -> Dict[str, float]:
        """B.3 (2026-05-14): 按 strategy 视角看占用 (与 bucket 视图区分)"""
        with self._lock:
            return dict(self._by_strategy)

    def is_observe_only(self) -> bool:
        """B.6 (2026-05-14): 公开访问 — 替代私有 _observe_only"""
        return self._observe_only

    def compute_scale_factor(self, strategy: str, position_pct: float) -> float:
        """Task #1 (2026-05-31): 计算按比例缩放系数

        当调用方希望 "总敞口超限 → 按比例缩放" 而非硬拒时使用.
        返回 [0.0, 1.0] 区间系数: 1.0 = 无需缩放; 0.0 = 完全拒绝.
        同时考虑 bucket cap 和 总敞口 cap, 取较小者.
        调用方可: 将 position_pct 乘以 该系数 后再 try_reserve.
        """
        with self._lock:
            if position_pct <= 0:
                return 1.0
            bucket = self._bucket_for(strategy)
            current = self._allocations.get(bucket, 0.0)
            cap = self._caps.get(bucket, 0.0)
            total = sum(self._allocations.values())

            # bucket 剩余
            bucket_headroom = max(0.0, cap - current) if cap > 0 else float('inf')
            # 总敞口剩余
            total_headroom = max(0.0, self._max_total - total)
            allowed = min(bucket_headroom, total_headroom, position_pct)
            scale = 0.0 if position_pct == 0 else max(0.0, min(1.0, allowed / position_pct))
            return scale

    def reset_and_reconcile(self, positions_by_strategy: Dict[str, float]):
        """
        P0-4 (2026-05-14): observe → enforce 切换前必调.

        observe 模式下账本可能透支 (BLOCKED 事件也记账了).
        切 enforce 前必须清零 + 用真实持仓重建, 否则会拒所有信号.
        """
        with self._lock:
            self._allocations = {b: 0.0 for b in self._caps}
            self._by_strategy = {}
            self._record_event("__reset__", 0.0, "ENFORCE_RESET", 0.0)
            self._persist_state_all()  # P1-7
        # 锁外调 (避免双重 lock)
        self.reconcile_with_positions(positions_by_strategy)
        logger.warning(
            f"[CapitalAllocator·RESET] 账本已清零 + reconcile, 现状: {self.snapshot().by_strategy}"
        )

    def reconcile_with_positions(self, positions_by_strategy: Dict[str, float]):
        """
        与真实持仓同步: positions_by_strategy = {strategy: total_position_pct}
        如发现账本与持仓加总不一致 → 用持仓修正 + 告警
        会先把按 strategy 的持仓汇总到 bucket, 然后修正 bucket 账本.
        """
        with self._lock:
            # 先把 strategy 维度的持仓汇总到 bucket 维度
            actual_by_bucket: Dict[str, float] = {}
            for strat, pct in positions_by_strategy.items():
                bucket = self._bucket_for(strat)
                actual_by_bucket[bucket] = actual_by_bucket.get(bucket, 0.0) + pct
            # 同步: 检查所有可能的 bucket
            all_buckets = set(self._allocations) | set(actual_by_bucket)
            for b in all_buckets:
                book = self._allocations.get(b, 0.0)
                actual = actual_by_bucket.get(b, 0.0)
                if abs(book - actual) > 0.005:  # 0.5% 容差
                    logger.warning(
                        f"[CapitalAllocator·RECONCILE] bucket={b} 账本={book:.3f} 实际={actual:.3f} → 修正"
                    )
                    self._allocations[b] = actual
                    self._record_event(b, 0.0, "RECONCILED", actual)
            # 同步 by_strategy 视图
            for strat, pct in positions_by_strategy.items():
                self._by_strategy[strat] = pct
            self._persist_state_all()  # P1-7

    def get_recent_events(self, n: int = 50) -> List[Dict]:
        """最近 N 条事件 (供 daily_review 读)"""
        with self._lock:
            return [
                {
                    "ts": e.timestamp,
                    "strategy": e.strategy,
                    "delta": e.delta,
                    "decision": e.decision,
                    "snapshot": e.snapshot_after,
                }
                for e in self._events[-n:]
            ]

    def _record_event(self, strategy: str, delta: float, decision: str, snapshot_after: float):
        e = _ReserveEvent(
            timestamp=time.time(),
            strategy=strategy,
            delta=delta,
            decision=decision,
            snapshot_after=snapshot_after,
        )
        self._events.append(e)
        if len(self._events) > self._max_event_history:
            self._events = self._events[-self._max_event_history:]
        # P1-7: persist ledger event (best-effort)
        if self._db_path:
            try:
                self._persist_event(e)
            except Exception as _pe:
                logger.debug(f"[CapitalAllocator] persist event failed: {_pe}")



    # ====================== P1-7: persistent ledger ======================
    def _init_persist_db(self, db_path: str) -> None:
        d = os.path.dirname(db_path)
        if d and not os.path.exists(d):
            os.makedirs(d, exist_ok=True)
        with self._db_lock:
            conn = sqlite3.connect(db_path, timeout=10.0)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA busy_timeout=5000")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS capital_state (
                        kind TEXT NOT NULL,
                        key  TEXT NOT NULL,
                        value REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        PRIMARY KEY (kind, key)
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS capital_ledger (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts REAL NOT NULL,
                        strategy TEXT NOT NULL,
                        delta REAL NOT NULL,
                        decision TEXT NOT NULL,
                        snapshot_after REAL NOT NULL
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_capital_ledger_ts ON capital_ledger(ts)")
                conn.commit()
            finally:
                conn.close()
        self._db_path = db_path

    def _load_persist_state(self) -> None:
        if not self._db_path:
            return
        with self._db_lock:
            conn = sqlite3.connect(self._db_path, timeout=10.0)
            try:
                cur = conn.execute("SELECT kind, key, value FROM capital_state")
                for kind, key, val in cur.fetchall():
                    try:
                        v = float(val)
                    except Exception:
                        continue
                    if kind == "bucket":
                        if key in self._allocations:
                            self._allocations[key] = max(0.0, v)
                    elif kind == "strategy":
                        self._by_strategy[key] = max(0.0, v)
            finally:
                conn.close()

    def _persist_state(self, bucket: str, strategy: Optional[str] = None) -> None:
        if not self._db_path:
            return
        ts = time.time()
        rows = [("bucket", bucket, float(self._allocations.get(bucket, 0.0)), ts)]
        if strategy is not None:
            rows.append(("strategy", strategy, float(self._by_strategy.get(strategy, 0.0)), ts))
        try:
            with self._db_lock:
                conn = sqlite3.connect(self._db_path, timeout=5.0)
                try:
                    conn.executemany(
                        "INSERT INTO capital_state(kind,key,value,updated_at) VALUES (?,?,?,?) "
                        "ON CONFLICT(kind,key) DO UPDATE SET value=excluded.value, "
                        "updated_at=excluded.updated_at",
                        rows,
                    )
                    conn.commit()
                finally:
                    conn.close()
        except Exception as e:
            logger.debug(f"[CapitalAllocator] _persist_state failed: {e}")

    def _persist_state_all(self) -> None:
        if not self._db_path:
            return
        ts = time.time()
        rows = [("bucket", b, float(v), ts) for b, v in self._allocations.items()]
        rows += [("strategy", s, float(v), ts) for s, v in self._by_strategy.items()]
        try:
            with self._db_lock:
                conn = sqlite3.connect(self._db_path, timeout=5.0)
                try:
                    conn.executemany(
                        "INSERT INTO capital_state(kind,key,value,updated_at) VALUES (?,?,?,?) "
                        "ON CONFLICT(kind,key) DO UPDATE SET value=excluded.value, "
                        "updated_at=excluded.updated_at",
                        rows,
                    )
                    conn.commit()
                finally:
                    conn.close()
        except Exception as e:
            logger.debug(f"[CapitalAllocator] _persist_state_all failed: {e}")

    def _persist_event(self, e) -> None:
        if not self._db_path:
            return
        with self._db_lock:
            conn = sqlite3.connect(self._db_path, timeout=5.0)
            try:
                conn.execute(
                    "INSERT INTO capital_ledger(ts,strategy,delta,decision,snapshot_after) "
                    "VALUES (?,?,?,?,?)",
                    (e.timestamp, e.strategy, e.delta, e.decision, e.snapshot_after),
                )
                conn.execute(
                    "DELETE FROM capital_ledger WHERE id <= "
                    "(SELECT MAX(id) FROM capital_ledger) - 5000"
                )
                conn.commit()
            finally:
                conn.close()

_singleton: Optional[CapitalAllocator] = None


def get_capital_allocator(caps: Optional[Dict[str, float]] = None,
                          max_total_pct: float = 0.90,
                          observe_only: bool = True,
                          db_path: Optional[str] = None) -> CapitalAllocator:
    """单例. 第一次调用必须传 caps. P1-7: db_path enables persistence."""
    global _singleton
    if _singleton is None:
        if caps is None:
            raise ValueError("CapitalAllocator 第一次构造必须传 caps")
        _singleton = CapitalAllocator(
            caps=caps, max_total_pct=max_total_pct,
            observe_only=observe_only, db_path=db_path,
        )
    return _singleton
