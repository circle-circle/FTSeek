"""
统一数据库访问层 (Repository Layer)

提供多数据库连接池管理，替代直接 sqlite3.connect() 调用。
解决高并发下的 database is locked 问题。

用法:
    # 方式1: 上下文管理器 (推荐, 自动归还连接)
    from app.storage.repository import get_db
    with get_db(db_path) as conn:
        conn.execute("SELECT * FROM trades")

    # 方式2: 手动获取/归还 (适合长方法)
    from app.storage.repository import get_pool
    pool = get_pool(db_path)
    conn = pool.get_connection()
    try:
        conn.execute("SELECT ...")
    finally:
        pool.return_connection(conn)

    # 方式3: 执行单次查询 (最简洁)
    from app.storage.repository import execute_query, fetch_all
    rows = fetch_all(db_path, "SELECT * FROM trades WHERE code=?", (code,))
    execute_query(db_path, "DELETE FROM trades WHERE id=?", (id,))
"""
import sqlite3
import logging
import threading
import queue
from contextlib import contextmanager
from typing import List, Dict, Any, Optional, Tuple
from pathlib import Path

from app.storage.database import ConnectionPool, get_sqlite_connection

logger = logging.getLogger("ftseek.storage.repository")

# ============================================================
#  连接池管理器 — 按 db_path 管理多个连接池
# ============================================================

class DatabasePoolManager:
    """
    管理多个 SQLite 连接池 (一个 db_path 对应一个池)
    
    自动创建/复用/清理连接池，避免每个模块独立管理。
    """
    
    _instance = None
    _lock = threading.Lock()
    
    def __init__(self):
        self._pools: Dict[str, ConnectionPool] = {}
        self._pool_lock = threading.Lock()
        self._default_pool_size = 5
        self._default_timeout = 30.0
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance
    
    def get_pool(self, db_path: str, pool_size: int = None, timeout: float = None) -> ConnectionPool:
        """
        获取指定数据库的连接池 (不存在则自动创建)
        
        Args:
            db_path: 数据库文件路径
            pool_size: 连接池大小 (默认5)
            timeout: 超时时间 (默认30秒)
        
        Returns:
            ConnectionPool 实例
        """
        db_path = str(Path(db_path).resolve())
        pool_size = pool_size or self._default_pool_size
        timeout = timeout or self._default_timeout
        
        if db_path not in self._pools:
            with self._pool_lock:
                # 双重检查
                if db_path not in self._pools:
                    # 确保目录存在
                    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
                    pool = ConnectionPool(db_path, pool_size=pool_size, timeout=timeout)
                    self._pools[db_path] = pool
                    logger.info(f"已为 {db_path} 创建连接池 (size={pool_size})")
        
        return self._pools[db_path]
    
    @contextmanager
    def connection(self, db_path: str):
        """
        上下文管理器: 获取连接 -> 使用 -> 自动归还
        
        Args:
            db_path: 数据库文件路径
        
        Yields:
            sqlite3.Connection
        """
        pool = self.get_pool(db_path)
        conn = pool.get_connection()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            pool.return_connection(conn)
    
    def execute(self, db_path: str, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """执行单条 SQL (自动获取/归还连接)"""
        with self.connection(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(sql, params)
            return cursor
    
    def fetch_one(self, db_path: str, sql: str, params: tuple = ()) -> Optional[Dict]:
        """查询单条记录"""
        with self.connection(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(sql, params)
            row = cursor.fetchone()
            return dict(row) if row else None
    
    def fetch_all(self, db_path: str, sql: str, params: tuple = ()) -> List[Dict]:
        """查询多条记录"""
        with self.connection(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(sql, params)
            return [dict(row) for row in cursor.fetchall()]
    
    def executemany(self, db_path: str, sql: str, params_list: List[tuple]) -> sqlite3.Cursor:
        """批量执行 SQL"""
        with self.connection(db_path) as conn:
            cursor = conn.cursor()
            cursor.executemany(sql, params_list)
            return cursor
    
    def close_all(self):
        """关闭所有连接池"""
        with self._pool_lock:
            for db_path, pool in self._pools.items():
                try:
                    pool.close_all()
                    logger.info(f"已关闭连接池: {db_path}")
                except Exception as e:
                    logger.error(f"关闭连接池失败 {db_path}: {e}")
            self._pools.clear()
    
    def get_stats(self) -> Dict[str, Dict]:
        """获取所有连接池的统计信息"""
        stats = {}
        for db_path, pool in self._pools.items():
            stats[db_path] = {
                "available_connections": pool.size,
                "pool_size": pool.pool_size,
            }
        return stats


# ============================================================
#  模块级便捷函数
# ============================================================

_pool_manager = DatabasePoolManager()


def get_pool(db_path: str, pool_size: int = None, timeout: float = None) -> ConnectionPool:
    """获取指定数据库的连接池"""
    return _pool_manager.get_pool(db_path, pool_size, timeout)


@contextmanager
def get_db(db_path: str):
    """
    上下文管理器: 从连接池获取连接，用完后自动归还
    
    with get_db(db_path) as conn:
        conn.execute("SELECT ...")
    """
    with _pool_manager.connection(db_path) as conn:
        yield conn


def execute_query(db_path: str, sql: str, params: tuple = ()) -> sqlite3.Cursor:
    """执行单条 SQL (自动提交)"""
    return _pool_manager.execute(db_path, sql, params)


def fetch_one(db_path: str, sql: str, params: tuple = ()) -> Optional[Dict]:
    """查询单条记录"""
    return _pool_manager.fetch_one(db_path, sql, params)


def fetch_all(db_path: str, sql: str, params: tuple = ()) -> List[Dict]:
    """查询多条记录"""
    return _pool_manager.fetch_all(db_path, sql, params)


def execute_many(db_path: str, sql: str, params_list: List[tuple]) -> sqlite3.Cursor:
    """批量执行"""
    return _pool_manager.executemany(db_path, sql, params_list)


def close_all_pools():
    """关闭所有连接池 (应用关闭时调用)"""
    _pool_manager.close_all()


# Alias for compatibility
shutdown_all = close_all_pools


def get_pool_stats() -> Dict[str, Dict]:
    """获取连接池统计"""
    return _pool_manager.get_stats()
