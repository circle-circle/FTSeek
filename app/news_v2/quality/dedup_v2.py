"""
FTSeek-US 新闻去重引擎 V2 — SimHash + 精确哈希双层去重

注意: SimHash 使用 63 位 (非 64 位) 以适配 SQLite INTEGER (有符号 64 位, 最大 2^63-1)
"""

import sqlite3
import time
import os
import threading
import logging
from typing import Optional, Dict

logger = logging.getLogger("ftseek_us.news_v2.dedup")
DEFAULT_DB_PATH = os.path.expanduser("~/.hermes/ftseek/data/news_dedup_us_v2.db")


class DedupEngineV2:
    def __init__(self, db_path: str = None, ttl_hours: int = 48):
        self.db_path = db_path or DEFAULT_DB_PATH
        self.ttl_seconds = ttl_hours * 3600
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()
        self._cleanup_expired()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS dedup_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title_hash TEXT UNIQUE NOT NULL,
                    simhash INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    title TEXT,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_expires ON dedup_records(expires_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_simhash ON dedup_records(simhash)")

    def _cleanup_expired(self):
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("DELETE FROM dedup_records WHERE expires_at < ?", (time.time(),))

    # DEB-20260903 #61: 去重查询增加 expires_at 过滤 — 原 Gate1/Gate2 不过滤过期记录,
    # 且 _cleanup_expired 仅在 __init__ 跑一次, 过期记录持续参与去重判定导致误判
    def _is_duplicate_conn(self, conn, title_hash: str, simhash: int,
                           source: str, title: str, similarity_threshold: float) -> bool:
        """在已有连接上判定重复 (调用方须持锁). 供 is_duplicate/check_and_mark 复用."""
        now = time.time()
        # Gate 1: 精确匹配 (仅未过期)
        cursor = conn.execute(
            "SELECT id, source FROM dedup_records WHERE title_hash = ? AND expires_at > ?",
            (title_hash, now)
        )
        if cursor.fetchone():
            logger.debug("精确去重: [%s] | title=%s", source, title[:50])
            return True

        # Gate 2: SimHash 模糊匹配 (仅未过期)
        hamming_threshold = int(63 * (1 - similarity_threshold))
        cursor = conn.execute(
            "SELECT simhash, source, title FROM dedup_records WHERE expires_at > ?", (now,)
        )
        for row in cursor:
            existing_simhash, existing_source, existing_title = row
            dist = self._hamming_distance(simhash, existing_simhash)
            if dist <= hamming_threshold:
                logger.debug("SimHash 去重: [%s] vs [%s] | dist=%d | title=%s",
                            existing_source, source, dist, title[:50])
                return True
        return False

    def _mark_as_seen_conn(self, conn, title_hash: str, simhash: int,
                           source: str, title: str):
        """在已有连接上写入记录 (调用方须持锁)."""
        now = time.time()
        simhash = simhash & 0x7FFFFFFFFFFFFFFF
        # DEB-20260904 #S8: 过期僵尸行(title_hash UNIQUE)会卡住 INSERT OR IGNORE —
        # _cleanup_expired 仅在 __init__ 跑一次, 过期行未被清理时同标题重写被静默
        # 吞掉, 该新闻在新鲜窗口内永远不被记录 → 同源重复放行。先删同行再过 INSERT。
        conn.execute(
            "DELETE FROM dedup_records WHERE title_hash = ? AND expires_at <= ?",
            (title_hash, now))
        conn.execute(
            """INSERT OR IGNORE INTO dedup_records
               (title_hash, simhash, source, title, created_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (title_hash, simhash, source, title[:200], now, now + self.ttl_seconds)
        )

    def is_duplicate(self, title_hash: str, simhash: int,
                     source: str = "", title: str = "",
                     similarity_threshold: float = 0.85) -> bool:
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                return self._is_duplicate_conn(
                    conn, title_hash, simhash, source, title, similarity_threshold)

    def mark_as_seen(self, title_hash: str, simhash: int,
                     source: str = "", title: str = ""):
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                self._mark_as_seen_conn(conn, title_hash, simhash, source, title)

    def check_and_mark(self, title_hash: str, simhash: int,
                       source: str = "", title: str = "",
                       similarity_threshold: float = 0.85) -> bool:
        # DEB-20260903 #62: 查重+写入收敛到同一把锁/同一连接 —
        # 原 is_duplicate/mark_as_seen 各自独立取锁, 两次锁释放间存在 TOCTOU 窗口,
        # 并发调用可产生重复记录
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                if self._is_duplicate_conn(conn, title_hash, simhash, source, title, similarity_threshold):
                    return False
                self._mark_as_seen_conn(conn, title_hash, simhash, source, title)
                return True

    def get_stats(self) -> Dict:
        with sqlite3.connect(self.db_path) as conn:
            total = conn.execute("SELECT COUNT(*) FROM dedup_records").fetchone()[0]
            valid = conn.execute("SELECT COUNT(*) FROM dedup_records WHERE expires_at > ?",
                                (time.time(),)).fetchone()[0]
            sources = {}
            for row in conn.execute("SELECT source, COUNT(*) FROM dedup_records GROUP BY source"):
                sources[row[0]] = row[1]
            return {"total_records": total, "valid_records": valid, "sources": sources, "ttl_hours": self.ttl_seconds // 3600}

    @staticmethod
    def _hamming_distance(h1: int, h2: int) -> int:
        x = h1 ^ h2
        d = 0
        while x:
            d += 1
            x &= x - 1
        return d
