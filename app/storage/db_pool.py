"""Unified SQLite connection pool with WAL mode and thread safety.

Problem: 199 scattered sqlite3.connect() calls across 15+ files cause:
  - No connection reuse (open/close per query)
  - No WAL/busy_timeout coordination → "database is locked"
  - Potential connection leaks (missing .close())
  - No central visibility into DB usage

Solution: DatabasePool singleton with:
  - Per-DB-name connection cache (thread-local)
  - Automatic WAL mode + busy_timeout
  - Context manager for safe open/close
  - Usage stats for observability

Usage:
  from app.storage.db_pool import get_db

  # Context manager (recommended - auto close)
  with get_db("ftseek_us") as conn:
      conn.execute("SELECT ...")

  # Or get a cached connection (caller must not close)
  conn = get_db("ftseek_us").connection()
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Optional, Generator
import time

logger = logging.getLogger("ftseek_us.db_pool")

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Default DB paths — ftseek_us respects TRADE_ENV=0 (live isolation)
# 2026-08-03 (A/B 隔离修复): FTSEEK_DB_FILE 环境变量可显式覆盖 ftseek_us DB 文件路径,
# 使 deep-stock-analysis skill 的 A/B 测试可将 PlaybookDB 指向临时库, 避免污染生产.
_ftseek_us_db = os.getenv(
    "FTSEEK_DB_FILE",
    "ftseek_us_live.db" if os.getenv("TRADE_ENV", "1").strip() == "0" else "ftseek_us.db",
)
_DB_PATHS: Dict[str, Path] = {
    "ftseek_us": PROJECT_ROOT / "data" / _ftseek_us_db,
    # [RFC-080 S1 2026-09-09] 研究库 pin — 毕业门禁/IC 读方专用, 恒 ftseek_us.db,
    # 不随 TRADE_ENV 漂移 (缺口1: 翻 TRADE_ENV=0 后门禁读 live 库 16 行 → 永判未毕业
    # → 降级 sim 下单但记录写 live = 自锁)。证据: sim 库 playbook_instructions 1918 行
    # (max_date 9/8) vs live 库 16 行 (止于 6/22)。详见
    # docs/PROPOSAL-20260909-cutover-dual-arm-routing.md + db_utils.get_research_db()。
    "ftseek_research": PROJECT_ROOT / "data" / "ftseek_us.db",
    "llm_audit": PROJECT_ROOT / "data" / "llm_audit.db",
    "rejections": PROJECT_ROOT / "data" / "rejections.db",
    "anomaly": PROJECT_ROOT / "data" / "anomaly_queue.db",
    "pdt": PROJECT_ROOT / "data" / "pdt_guard.db",
    "proposals": PROJECT_ROOT / "data" / "proposals.db",
    "review": PROJECT_ROOT / "data" / "ftseek_us_review.db",
    "decisions": PROJECT_ROOT / "data" / "decisions.db",
    "shadow_logs": PROJECT_ROOT / "data" / "shadow_logs.db",
}


class _DBStats:
    """Lightweight usage stats (not thread-safe, best-effort)."""

    def __init__(self):
        self.opens: int = 0
        self.closes: int = 0
        self.errors: int = 0
        self._last_open: float = 0

    def record_open(self):
        self.opens += 1
        self._last_open = time.time()

    def record_close(self):
        self.closes += 1

    def record_error(self):
        self.errors += 1

    def summary(self) -> dict:
        return {
            "opens": self.opens,
            "closes": self.closes,
            "errors": self.errors,
            "leaked": self.opens - self.closes,
            "last_open_age_sec": round(time.time() - self._last_open, 1) if self._last_open else None,
        }


class DatabasePool:
    """Thread-safe SQLite connection pool with WAL mode.

    Each DB name maps to a thread-local cached connection.
    Connections are configured with:
      - WAL journal mode (concurrent reads)
      - busy_timeout=5000ms (wait for locks)
      - row_factory=sqlite3.Row (dict-like access)
    """

    def __init__(self):
        self._local = threading.local()
        self._stats: Dict[str, _DBStats] = {}
        self._lock = threading.Lock()
        self._custom_paths: Dict[str, Path] = {}

    def register_path(self, name: str, path: Path):
        """Register a custom DB path (for non-standard locations)."""
        with self._lock:
            self._custom_paths[name] = path

    def _get_path(self, name: str) -> Path:
        if name in self._custom_paths:
            return self._custom_paths[name]
        if name in _DB_PATHS:
            return _DB_PATHS[name]
        raise ValueError(f"Unknown DB: {name}. Register with register_path() first.")

    def _get_stats(self, name: str) -> _DBStats:
        if name not in self._stats:
            with self._lock:
                if name not in self._stats:
                    self._stats[name] = _DBStats()
        return self._stats[name]

    def _get_cached(self, name: str) -> sqlite3.Connection:
        """Get or create a thread-local cached connection."""
        cache_key = f"_db_{name}"
        conn = getattr(self._local, cache_key, None)

        if conn is not None:
            try:
                conn.execute("SELECT 1")
                return conn
            except Exception:
                # Connection stale, recreate
                try:
                    conn.close()
                except Exception:
                    pass
                setattr(self._local, cache_key, None)

        path = self._get_path(name)
        conn = self._create_connection(path)
        setattr(self._local, cache_key, conn)
        self._get_stats(name).record_open()
        return conn

    @staticmethod
    def _create_connection(path: Path) -> sqlite3.Connection:
        """Create a new connection with optimal settings."""
        conn = sqlite3.connect(
            str(path),
            timeout=10.0,           # 10s busy timeout
            check_same_thread=False, # thread-local cache handles safety
        )
        conn.row_factory = sqlite3.Row
        # WAL mode for concurrent reads
        conn.execute("PRAGMA journal_mode=WAL")
        # 5s busy timeout (belt and suspenders with connect timeout)
        conn.execute("PRAGMA busy_timeout=5000")
        # Synchronous NORMAL for WAL (safe + fast)
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def connection(self, name: str = "ftseek_us") -> sqlite3.Connection:
        """Get a cached connection (DO NOT close manually).

        The connection is thread-local and reused across calls.
        Use this for quick one-off queries in hot paths.
        """
        return self._get_cached(name)

    @contextmanager
    def connect(self, name: str = "ftseek_us") -> Generator[sqlite3.Connection, None, None]:
        """Context manager: get a fresh connection, auto-close on exit.

        Use this for:
          - Transactions that need explicit commit/rollback
          - Operations that modify many rows
          - Code paths where you want guaranteed cleanup
        """
        path = self._get_path(name)
        stats = self._get_stats(name)
        conn = self._create_connection(path)
        stats.record_open()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            stats.record_error()
            raise
        finally:
            conn.close()
            stats.record_close()

    def close_all(self):
        """Close all cached connections (call on shutdown)."""
        # Thread-local cache is per-thread, so this only closes current thread's
        for key in list(vars(self._local).keys()):
            if key.startswith("_db_"):
                conn = getattr(self._local, key, None)
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    setattr(self._local, key, None)

    def stats(self) -> Dict[str, dict]:
        """Return usage stats for all DBs."""
        return {name: s.summary() for name, s in self._stats.items()}


# ─── Global singleton ───
_pool: Optional[DatabasePool] = None
_pool_lock = threading.Lock()


def get_pool() -> DatabasePool:
    """Get or create the global DatabasePool singleton."""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = DatabasePool()
    return _pool


def get_db_path(name: str) -> Path:
    """命名库文件路径的公开 SSOT 读取口 (2026-08-30 R-3 收敛).

    llm_audit 等库路径原有三处平行定义 (db_pool._DB_PATHS / db_utils.get_llm_audit_db /
    audit_db._get_db_path), 此处为唯一定义的公开出口; 消费方一律 import 本函数,
    不得再行内拼 "data/xxx.db"。含 register_path 自定义覆盖 (经池单例解析)。
    """
    return get_pool()._get_path(name)


def get_db(name: str = "ftseek_us") -> DatabasePool:
    """Convenience: returns pool for chaining.

    Usage:
      get_db("ftseek_us").connection().execute("SELECT ...")
      with get_db().connect() as conn: ...
    """
    return get_pool()


# Module-level convenience
def db_connect(name: str = "ftseek_us") -> sqlite3.Connection:
    """Get a cached connection (shorthand for get_pool().connection())."""
    return get_pool().connection(name)


@contextmanager
def db_transaction(name: str = "ftseek_us") -> Generator[sqlite3.Connection, None, None]:
    """Context manager for a transactional connection."""
    with get_pool().connect(name) as conn:
        yield conn
