"""
全局新闻推送去重模块

基于 SQLite 实现跨进程、跨运行的新闻去重。
所有推送模块在发送钉钉消息前应调用 check_and_mark() 检查。

用法:
    from app.utils.dedup import NewsDedup
    
    dedup = NewsDedup(db_path="data/news_dedup.db", ttl_hours=2)
    
    # 推送前检查
    if dedup.check_and_mark(news_hash):
        # 未重复，可以推送
        send_to_dingtalk(news)
    else:
        # 已推送过，跳过
        logger.info(f"新闻已推送过，跳过: {news_hash}")
"""

import hashlib
import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger("ftseek.utils.dedup")


class NewsDedup:
    """基于 SQLite 的全局新闻去重器"""
    
    def __init__(self, db_path: str = None, ttl_hours: int = 2):
        """
        Args:
            db_path: SQLite 数据库路径
            ttl_hours: 去重有效期（小时），默认 2 小时
        """
        if db_path is None:
            base = Path(__file__).resolve().parent.parent.parent / "data"
            base.mkdir(parents=True, exist_ok=True)
            db_path = str(base / "news_dedup.db")
        
        self.db_path = db_path
        self.ttl_hours = ttl_hours
        self._lock = threading.Lock()
        
        self._init_db()
        self.cleanup_old()
        
        logger.info(f"新闻去重器已初始化: {db_path} (TTL={ttl_hours}h)")
    
    def _get_conn(self) -> sqlite3.Connection:
        """获取数据库连接（线程安全）"""
        return sqlite3.connect(self.db_path, timeout=10, check_same_thread=False)
    
    def _init_db(self):
        """初始化数据库表"""
        conn = self._get_conn()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pushed_news (
                    news_hash TEXT PRIMARY KEY,
                    title TEXT,
                    source TEXT,
                    pushed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    channel TEXT DEFAULT 'unknown'
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_pushed_at 
                ON pushed_news(pushed_at)
            """)
            conn.commit()
        finally:
            conn.close()
    
    def compute_hash(self, title: str, source: str = "") -> str:
        """计算新闻的哈希值（基于标题+来源）"""
        raw = f"{source}::{title}".strip()
        return hashlib.md5(raw.encode("utf-8")).hexdigest()
    
    def is_duplicate(self, title: str, source: str = "") -> bool:
        """检查新闻是否已推送过"""
        news_hash = self.compute_hash(title, source)
        return self.is_duplicate_hash(news_hash)
    
    def is_duplicate_hash(self, news_hash: str) -> bool:
        """检查哈希是否已存在于去重库中"""
        conn = self._get_conn()
        try:
            cur = conn.execute(
                "SELECT 1 FROM pushed_news WHERE news_hash = ? AND pushed_at > datetime('now', ?)",
                (news_hash, f'-{self.ttl_hours} hours')
            )
            return cur.fetchone() is not None
        finally:
            conn.close()
    
    def mark_pushed(self, title: str, source: str = "", channel: str = "unknown",
                    news_hash: str = None) -> bool:
        """标记新闻已推送"""
        if news_hash is None:
            news_hash = self.compute_hash(title, source)
        
        conn = self._get_conn()
        try:
            conn.execute(
                """INSERT OR REPLACE INTO pushed_news 
                   (news_hash, title, source, pushed_at, channel) 
                   VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?)""",
                (news_hash, title[:200], source[:50], channel)
            )
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"标记已推送新闻失败: {e}")
            return False
        finally:
            conn.close()
    
    def check_and_mark(self, title: str, source: str = "", 
                       channel: str = "unknown") -> bool:
        """
        原子操作：检查并标记
        
        Returns:
            True: 未重复，已标记为新推送（可以发送）
            False: 重复，跳过
        """
        with self._lock:
            if self.is_duplicate(title, source):
                return False
            self.mark_pushed(title, source, channel)
            return True
    
    def cleanup_old(self):
        """清理过期的去重记录"""
        conn = self._get_conn()
        try:
            cur = conn.execute(
                "DELETE FROM pushed_news WHERE pushed_at <= datetime('now', ?)",
                (f'-{self.ttl_hours} hours',)
            )
            conn.commit()
            deleted = cur.rowcount
            if deleted > 0:
                logger.debug(f"清理 {deleted} 条过期去重记录")
            return deleted
        finally:
            conn.close()
    
    def get_stats(self) -> dict:
        """获取去重统计"""
        conn = self._get_conn()
        try:
            total = conn.execute(
                "SELECT COUNT(*) FROM pushed_news"
            ).fetchone()[0]
            
            active = conn.execute(
                "SELECT COUNT(*) FROM pushed_news WHERE pushed_at > datetime('now', ?)",
                (f'-{self.ttl_hours} hours',)
            ).fetchone()[0]
            
            by_channel = {}
            for row in conn.execute(
                "SELECT channel, COUNT(*) FROM pushed_news WHERE pushed_at > datetime('now', ?) GROUP BY channel",
                (f'-{self.ttl_hours} hours',)
            ):
                by_channel[row[0]] = row[1]
            
            return {
                "total_records": total,
                "active_records": active,
                "ttl_hours": self.ttl_hours,
                "by_channel": by_channel,
            }
        finally:
            conn.close()


# 全局单例
_instance: Optional[NewsDedup] = None
_instance_lock = threading.Lock()


def get_dedup(db_path: str = None, ttl_hours: int = 2) -> NewsDedup:
    """获取全局去重器单例"""
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = NewsDedup(db_path, ttl_hours)
    return _instance


def reset_dedup():
    """重置全局单例（测试用）"""
    global _instance
    with _instance_lock:
        if _instance:
            _instance = None
