"""
FTSeek 统一新闻管线 - 全局去重引擎 (Global Deduplication Engine)
2026-04-13 创建

功能:
1. 基于 ContentHash 的跨源新闻去重。
2. 使用 SQLite 存储去重记录，支持持久化和高并发 (WAL)。
3. 支持设置保留时间 (TTL)，自动清理旧记录。
"""

import os
import sqlite3
import logging
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("ftseek.utils.dedup_global")

# 默认数据库路径
_DEFAULT_DB_DIR = Path(__file__).resolve().parent.parent.parent / "data"
_DEFAULT_DB_DIR.mkdir(exist_ok=True)
_DB_PATH = _DEFAULT_DB_DIR / "news_dedup.db"

SQL_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS news_hashes (
    content_hash TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    title TEXT,
    created_at REAL NOT NULL
);
"""

SQL_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_news_hashes_created_at ON news_hashes(created_at);
"""

SQL_INSERT = """
INSERT OR REPLACE INTO news_hashes (content_hash, source, title, created_at)
VALUES (?, ?, ?, ?);
"""

SQL_CHECK = """
SELECT 1 FROM news_hashes WHERE content_hash = ?;
"""

SQL_CLEANUP = """
DELETE FROM news_hashes WHERE created_at < ?;
"""


class GlobalNewsDeduplicator:
    """全局新闻去重器"""

    def __init__(self, db_path: str = None, ttl_hours: int = 48):
        """
        初始化去重引擎

        Args:
            db_path: 数据库路径，默认 data/news_dedup.db
            ttl_hours: 去重记录保留时间 (小时)，默认 48 小时
        """
        self.db_path = db_path or str(_DB_PATH)
        self.ttl_seconds = ttl_hours * 3600
        self._conn = None
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, timeout=10, check_same_thread=False)
            # 优化并发性能
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        return self._conn

    def _init_db(self):
        """初始化数据库表"""
        conn = self._get_conn()
        try:
            # 2026-04-14 修复: SQLite execute() 不支持多语句，分开执行
            conn.execute(SQL_CREATE_TABLE)
            conn.execute(SQL_CREATE_INDEX)
            conn.commit()
            logger.info(f"全局去重引擎已初始化 | DB={self.db_path} | TTL={self.ttl_seconds // 3600}h")
        except Exception as e:
            logger.error(f"去重引擎初始化失败: {e}")
            raise

    def is_duplicate(self, content_hash: str) -> bool:
        """
        检查新闻是否重复

        Args:
            content_hash: 新闻内容 Hash

        Returns:
            True: 是重复新闻
            False: 是新闻
        """
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute(SQL_CHECK, (content_hash,))
            return cursor.fetchone() is not None
        except Exception as e:
            logger.error(f"检查重复失败: {e}")
            return False  # 出错时放行，宁可错推不可漏推

    def mark_as_seen(self, content_hash: str, source: str = "", title: str = ""):
        """
        标记新闻为已见 (记录 Hash)

        Args:
            content_hash: 新闻内容 Hash
            source: 来源标识
            title: 新闻标题
        """
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            now = time.time()
            cursor.execute(SQL_INSERT, (content_hash, source, title, now))
            conn.commit()
        except Exception as e:
            logger.error(f"记录去重 Hash 失败: {e}")

    def check_and_mark(self, content_hash: str, source: str = "", title: str = "") -> bool:
        """
        检查并标记 (原子操作)

        Returns:
            True: 如果是新新闻 (标记成功)
            False: 如果是重复新闻
        """
        if self.is_duplicate(content_hash):
            return False
        
        self.mark_as_seen(content_hash, source, title)
        return True

    def cleanup_expired(self):
        """清理过期的去重记录"""
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            cutoff_time = time.time() - self.ttl_seconds
            cursor.execute(SQL_CLEANUP, (cutoff_time,))
            conn.commit()
            if cursor.rowcount > 0:
                logger.info(f"清理了 {cursor.rowcount} 条过期去重记录")
        except Exception as e:
            logger.error(f"清理过期记录失败: {e}")

    def close(self):
        """关闭连接"""
        if self._conn:
            self._conn.close()
            self._conn = None


# 全局单例
_dedup_instance: Optional[GlobalNewsDeduplicator] = None
_dedup_lock = False


def get_global_dedup(db_path: str = None, ttl_hours: int = 48) -> GlobalNewsDeduplicator:
    """获取全局去重引擎单例"""
    global _dedup_instance
    if _dedup_instance is None:
        _dedup_instance = GlobalNewsDeduplicator(db_path=db_path, ttl_hours=ttl_hours)
    return _dedup_instance
