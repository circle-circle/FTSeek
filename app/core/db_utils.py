"""
Centralized SQLite connection manager for FTSeek-US.

Replaces scattered sqlite3.connect() calls with safe context managers.
Prevents connection leaks, ensures proper cleanup on exceptions.

Usage:
    from app.core.db_utils import db_connect, get_main_db, get_trade_records_db

    # Context manager (preferred - auto-closes)
    with db_connect(get_main_db()) as conn:
        rows = conn.execute("SELECT ...").fetchall()

    # For class __init__ (long-lived connections)
    self._conn = db_connect_long(get_main_db())

Global Safety Hook (2026-06-16):
    All sqlite3.connect() calls are automatically patched with WAL + busy_timeout.
    This ensures 211+ raw calls across 90+ files get safe defaults without changes.
"""

import os
import sqlite3
import logging
import threading
from pathlib import Path
from contextlib import contextmanager
from typing import Optional

logger = logging.getLogger(__name__)

# Default paths
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DATA_DIR = _PROJECT_ROOT / "data"


# ═══════════════════════════════════════════════════════════════════
#  Global Safety Hook — monkey-patch sqlite3.connect
#  All sqlite3.connect() calls automatically get WAL + busy_timeout
# ═══════════════════════════════════════════════════════════════════

_original_connect = sqlite3.connect


def _safe_connect(*args, **kwargs):
    """Wraps sqlite3.connect to apply WAL + busy_timeout on every connection."""
    conn = _original_connect(*args, **kwargs)
    try:
        db_path = str(args[0]) if args else ""
        # Skip pragmas for in-memory databases
        if ":memory:" not in db_path:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
    except Exception:
        pass  # Pragmas are best-effort; never block connection
    return conn


sqlite3.connect = _safe_connect


def _parse_trade_env_from_file() -> bool:
    """从 .env.us 解析 TRADE_ENV (0=live 库, 1=sim 库). 无文件则回落环境变量."""
    env_file = _PROJECT_ROOT / ".env.us"
    if env_file.exists():
        try:
            with open(env_file) as f:
                for line in f:
                    if line.strip().startswith("TRADE_ENV="):
                        return line.strip().split("=", 1)[1].strip() == "0"
        except OSError:
            pass
    return os.getenv("TRADE_ENV", "1").strip() == "0"


# ── R-3 (2026-08-23 DEB-20260823): 进程内固化 ──
# 原实现每次调用重读 .env.us (docstring 自述"无需重启进程"), 而 broker 路由用
# os.getenv("AUTO_TRADE_ACCOUNT") (position_sync.py:576) 需重启 → 两者不同源。
# 后果: 运行期把 .env.us 的 TRADE_ENV 改成 0, get_trade_records_db() 立刻指向
# ftseek_us_live.db 而 broker 仍是 SIMULATE; 此时 monitor_playbook_sim_exits
# 查到 rows=[] 直接 return 0 (pipeline.py:6890) → 全部在仓静默失去止损监控, 零告警。
# 这是"三态无状态机"最短的一条自毁路径, 且此前零测试覆盖。现固化为启动值。
_LIVE_MODE_CACHE: Optional[bool] = None
_LIVE_MODE_LOCK = threading.Lock()


def _assert_env_routing_consistent(is_live_db: bool) -> None:
    """DB 路由与 broker 路由一致性断言.

    只告警不抛异常 — 大量只读脚本/测试会 import 本模块, 启动期硬失败会误伤它们;
    真正的硬阻断放在下单路径 (playbook_service 的三态门) 更合适。
    """
    _acct = os.getenv("AUTO_TRADE_ACCOUNT", "sim").strip().lower()
    _broker_is_real = (_acct == "real")
    if is_live_db != _broker_is_real:
        logger.critical(
            "🚨 [ENV_ROUTING_MISMATCH] DB 路由与 broker 路由不一致: "
            "TRADE_ENV→%s 库, AUTO_TRADE_ACCOUNT=%s→%s 账户。"
            "此状态下账本与实际成交账户分离, 退出监控可能查不到在仓持仓 (裸仓风险)。"
            "请统一 .env.us 两处后重启 ftseek-us。",
            "live" if is_live_db else "sim", _acct,
            "REAL" if _broker_is_real else "SIMULATE",
        )


def is_live_mode() -> bool:
    """判断是否为实盘模式 (R-3: 进程内固化, 首次调用后不再重读文件).

    如确需运行时切换 (仅测试/迁移场景), 设 FTSEEK_ALLOW_RUNTIME_TRADE_ENV_SWITCH=1。
    """
    global _LIVE_MODE_CACHE
    if os.getenv("FTSEEK_ALLOW_RUNTIME_TRADE_ENV_SWITCH", "").strip() == "1":
        return _parse_trade_env_from_file()
    if _LIVE_MODE_CACHE is None:
        with _LIVE_MODE_LOCK:
            if _LIVE_MODE_CACHE is None:
                _resolved = _parse_trade_env_from_file()
                _assert_env_routing_consistent(_resolved)
                _LIVE_MODE_CACHE = _resolved
    return _LIVE_MODE_CACHE


def get_main_db() -> Path:
    """Get path to main ftseek_us.db (live: ftseek_us_live.db)"""
    if is_live_mode():
        return _DATA_DIR / "ftseek_us_live.db"
    return _DATA_DIR / "ftseek_us.db"


def get_trade_records_db() -> Path:
    """Get path to trade_records.db (alias for main db in US, live: ftseek_us_live.db)"""
    if is_live_mode():
        return _DATA_DIR / "ftseek_us_live.db"
    return _DATA_DIR / "ftseek_us.db"


def get_rejections_db() -> Path:
    """Get path to rejections.db"""
    return _DATA_DIR / "rejections.db"


def get_llm_audit_db() -> Path:
    """Get path to llm_audit.db (R-3 2026-08-30: 收敛到 db_pool 单一定义)"""
    from app.storage.db_pool import get_db_path
    return get_db_path("llm_audit")


@contextmanager
def db_connect(db_path: Optional[Path] = None, timeout: float = 10.0):
    """
    Safe SQLite connection context manager.

    - Auto-closes on exit (even on exceptions)
    - Sets WAL mode for better concurrent access
    - Enables foreign keys
    - Sets row_factory to sqlite3.Row for dict-like access

    Usage:
        with db_connect(get_main_db()) as conn:
            rows = conn.execute("SELECT ...").fetchall()
    """
    if db_path is None:
        db_path = get_main_db()

    conn = sqlite3.connect(str(db_path), timeout=timeout)
    conn.row_factory = sqlite3.Row
    try:
        # WAL mode for concurrent read/write
        conn.execute("PRAGMA journal_mode=WAL")
        # Critical #3 (2026-06-02): 语句级锁等待, 避免 "database is locked"
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def db_connect_long(db_path: Optional[Path] = None, timeout: float = 10.0):
    """
    Create a long-lived SQLite connection (for class __init__).

    Caller is responsible for calling conn.close() in __del__ or cleanup method.
    Sets same pragmas as db_connect().

    Usage:
        class MyClass:
            def __init__(self):
                self._conn = db_connect_long(get_main_db())

            def __del__(self):
                if hasattr(self, '_conn') and self._conn:
                    self._conn.close()
    """
    if db_path is None:
        db_path = get_main_db()

    conn = sqlite3.connect(str(db_path), timeout=timeout, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    # Critical #3 (2026-06-02): 语句级锁等待, 避免 "database is locked"
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def safe_sqlite_connect(db_path: Optional[Path] = None, timeout: float = 10.0):
    """Alias for db_connect_long() — 统一推荐名."""
    return db_connect_long(db_path, timeout=timeout)


def safe_query(db_path: Path, sql: str, params: tuple = (), fetchone: bool = False):
    """
    One-shot query helper - connect, execute, return, close.

    For simple read-only queries where you don't need a persistent connection.

    Usage:
        row = safe_query(get_main_db(), "SELECT * FROM positions WHERE symbol=?", ("AAPL",), fetchone=True)
        rows = safe_query(get_main_db(), "SELECT * FROM positions")
    """
    with db_connect(db_path) as conn:
        if fetchone:
            return conn.execute(sql, params).fetchone()
        return conn.execute(sql, params).fetchall()


def safe_execute(db_path: Path, sql: str, params: tuple = ()) -> int:
    """
    One-shot execute helper - connect, execute, commit, return rowcount.

    Usage:
        rows_affected = safe_execute(get_main_db(), "DELETE FROM positions WHERE symbol=?", ("AAPL",))
    """
    with db_connect(db_path) as conn:
        cursor = conn.execute(sql, params)
        return cursor.rowcount
