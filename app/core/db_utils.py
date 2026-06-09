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
"""

import os
import sqlite3
import logging
from pathlib import Path
from contextlib import contextmanager
from typing import Optional

logger = logging.getLogger(__name__)

# Default paths
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DATA_DIR = _PROJECT_ROOT / "data"


def is_live_mode() -> bool:
    """判断是否为实盘模式 (从 .env.us 实时读取, 无需重启进程)"""
    env_file = _PROJECT_ROOT / ".env.us"
    if env_file.exists():
        with open(env_file) as f:
            for line in f:
                if line.strip().startswith("TRADE_ENV="):
                    return line.strip().split("=", 1)[1].strip() == "0"
    return os.getenv("TRADE_ENV", "1").strip() == "0"


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
    """Get path to llm_audit.db"""
    return _DATA_DIR / "llm_audit.db"


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
