"""
跨市场事件缓存模块 (Cross-Market Event Cache)

功能:
1. 持久化美股交易时段的事件 (新闻、价格异动、财报等)
2. 次日港股盘前分析时读取缓存
3. 自动清理过期事件 (24 小时 TTL)
4. 支持按股票代码查询关联事件

架构:
  美股交易时段 (21:30-04:00 HKT)
      ↓
  [EventCache.write()] 写入事件
      ↓
  SQLite 持久化 (data/cross_market_events.db)
      ↓
  次日港股盘前 (08:30 HKT)
      ↓
  [EventCache.read_for_premarket()] 读取事件
      ↓
  注入 LLM 盘前分析 prompt

更新时间: 2026-04-13
"""

import os
import json
import time
import sqlite3
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field, asdict
from pathlib import Path

from app.storage.repository import get_pool as _get_pool  # B3-13 连接池

logger = logging.getLogger("ftseek.cross_market_cache")


class _PooledConnProxy:
    """B3-13：sqlite3.Connection 的 close 是只读属性，无法 monkey-patch，
    因此用 Proxy 捕获 close 改为返还到池；其他属性透传。"""
    __slots__ = ("_pool", "_conn", "_closed")

    def __init__(self, pool, conn):
        object.__setattr__(self, "_pool", pool)
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "_closed", False)

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __setattr__(self, name, value):
        setattr(self._conn, name, value)

    def close(self):
        if not self._closed:
            try:
                self._pool.return_connection(self._conn)
            finally:
                object.__setattr__(self, "_closed", True)

    def __enter__(self):
        return self._conn

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            try:
                self._conn.commit()
            except Exception:
                pass
        else:
            try:
                self._conn.rollback()
            except Exception:
                pass
        self.close()
        return False

DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "cross_market_events.db"
)


@dataclass
class CrossMarketEvent:
    """跨市场事件"""
    event_id: str
    timestamp: float
    event_type: str  # "price_move", "earnings", "news", "macro"
    us_code: str
    hk_codes: List[str]
    title: str
    content: str
    impact_score: float = 0.0  # -1.0 到 1.0
    processed: bool = False
    metadata: Dict = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict) -> 'CrossMarketEvent':
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class CrossMarketEventCache:
    """跨市场事件缓存管理器"""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _get_conn(self):
        """B3-13：从连接池借出一个连接，返回 Proxy（close 自动返还池）。保留旧调用方写法。"""
        pool = _get_pool(self.db_path)
        conn = pool.get_connection()
        return _PooledConnProxy(pool, conn)

    def _init_db(self):
        """初始化数据库"""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)

        conn = self._get_conn()
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cross_market_events (
                event_id TEXT PRIMARY KEY,
                timestamp REAL NOT NULL,
                event_type TEXT NOT NULL,
                us_code TEXT NOT NULL,
                hk_codes TEXT NOT NULL,  -- JSON 数组
                title TEXT NOT NULL,
                content TEXT,
                impact_score REAL DEFAULT 0.0,
                processed INTEGER DEFAULT 0,
                metadata TEXT,  -- JSON
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_timestamp 
            ON cross_market_events (timestamp)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_us_code 
            ON cross_market_events (us_code)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_processed 
            ON cross_market_events (processed)
        """)
        conn.commit()
        conn.close()

        logger.info(f"[CrossMarketEventCache] 数据库初始化完成: {self.db_path}")

    def write(self, event: CrossMarketEvent) -> bool:
        """
        写入跨市场事件

        Args:
            event: 事件对象

        Returns:
            是否写入成功
        """
        try:
            conn = self._get_conn()
            conn.execute("""
                INSERT OR REPLACE INTO cross_market_events 
                (event_id, timestamp, event_type, us_code, hk_codes, 
                 title, content, impact_score, processed, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                event.event_id,
                event.timestamp,
                event.event_type,
                event.us_code,
                json.dumps(event.hk_codes, ensure_ascii=False),
                event.title,
                event.content,
                event.impact_score,
                1 if event.processed else 0,
                json.dumps(event.metadata, ensure_ascii=False),
            ))
            conn.commit()
            conn.close()

            logger.info(
                f"[CrossMarketEventCache] 事件已写入: {event.event_id} | "
                f"{event.us_code} → {event.hk_codes} | {event.event_type}"
            )
            return True

        except Exception as e:
            logger.error(f"[CrossMarketEventCache] 事件写入失败: {e}")
            return False

    def read_for_premarket(self, hours: int = 12) -> List[CrossMarketEvent]:
        """
        读取用于盘前分析的事件

        Args:
            hours: 获取最近 N 小时的事件

        Returns:
            事件列表 (按影响分数排序)
        """
        try:
            cutoff = time.time() - (hours * 3600)

            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("""
                SELECT * FROM cross_market_events 
                WHERE timestamp > ?
                ORDER BY impact_score DESC, timestamp DESC
                LIMIT 50
            """, (cutoff,))

            events = []
            for row in cursor:
                event = CrossMarketEvent(
                    event_id=row['event_id'],
                    timestamp=row['timestamp'],
                    event_type=row['event_type'],
                    us_code=row['us_code'],
                    hk_codes=json.loads(row['hk_codes']),
                    title=row['title'],
                    content=row['content'],
                    impact_score=row['impact_score'],
                    processed=bool(row['processed']),
                    metadata=json.loads(row['metadata']) if row['metadata'] else {},
                )
                events.append(event)

            conn.close()

            # 标记为已处理
            if events:
                self._mark_processed([e.event_id for e in events])

            logger.info(
                f"[CrossMarketEventCache] 读取 {len(events)} 个事件 "
                f"(最近 {hours} 小时)"
            )
            return events

        except Exception as e:
            logger.error(f"[CrossMarketEventCache] 读取事件失败: {e}")
            return []

    def read_by_hk_code(self, hk_code: str, hours: int = 24) -> List[CrossMarketEvent]:
        """
        根据港股代码读取关联事件

        Args:
            hk_code: 港股代码
            hours: 时间范围

        Returns:
            事件列表
        """
        try:
            cutoff = time.time() - (hours * 3600)

            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("""
                SELECT * FROM cross_market_events 
                WHERE timestamp > ? 
                AND hk_codes LIKE ?
                ORDER BY timestamp DESC
            """, (cutoff, f'%{hk_code}%'))

            events = []
            for row in cursor:
                event = CrossMarketEvent(
                    event_id=row['event_id'],
                    timestamp=row['timestamp'],
                    event_type=row['event_type'],
                    us_code=row['us_code'],
                    hk_codes=json.loads(row['hk_codes']),
                    title=row['title'],
                    content=row['content'],
                    impact_score=row['impact_score'],
                    processed=bool(row['processed']),
                    metadata=json.loads(row['metadata']) if row['metadata'] else {},
                )
                events.append(event)

            conn.close()
            return events

        except Exception as e:
            logger.error(f"[CrossMarketEventCache] 按代码读取事件失败: {e}")
            return []

    def _mark_processed(self, event_ids: List[str]):
        """标记事件为已处理"""
        if not event_ids:
            return

        try:
            conn = self._get_conn()
            placeholders = ','.join('?' for _ in event_ids)
            conn.execute(f"""
                UPDATE cross_market_events 
                SET processed = 1 
                WHERE event_id IN ({placeholders})
            """, event_ids)
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"[CrossMarketEventCache] 标记已处理失败: {e}")

    def cleanup_expired(self, hours: int = 48):
        """清理过期事件"""
        try:
            cutoff = time.time() - (hours * 3600)

            conn = self._get_conn()
            cursor = conn.execute("""
                DELETE FROM cross_market_events WHERE timestamp < ?
            """, (cutoff,))
            deleted = cursor.rowcount
            conn.commit()
            conn.close()

            if deleted > 0:
                logger.info(f"[CrossMarketEventCache] 清理 {deleted} 个过期事件")

        except Exception as e:
            logger.error(f"[CrossMarketEventCache] 清理过期事件失败: {e}")

    def get_stats(self) -> Dict:
        """获取缓存统计"""
        try:
            conn = self._get_conn()
            cursor = conn.execute("SELECT COUNT(*) FROM cross_market_events")
            total = cursor.fetchone()[0]

            cursor = conn.execute(
                "SELECT COUNT(*) FROM cross_market_events WHERE processed = 0"
            )
            unprocessed = cursor.fetchone()[0]

            cursor = conn.execute(
                "SELECT COUNT(*) FROM cross_market_events WHERE timestamp > ?",
                (time.time() - 86400,)
            )
            last_24h = cursor.fetchone()[0]

            conn.close()

            return {
                "total_events": total,
                "unprocessed": unprocessed,
                "last_24h": last_24h,
            }

        except Exception as e:
            logger.error(f"[CrossMarketEventCache] 获取统计失败: {e}")
            return {}


# ============================================================
#  全局单例
# ============================================================
_cache: Optional[CrossMarketEventCache] = None


def get_cross_market_event_cache(db_path: str = None) -> CrossMarketEventCache:
    """获取跨市场事件缓存单例"""
    global _cache
    if _cache is None:
        if db_path:
            _cache = CrossMarketEventCache(db_path)
        else:
            _cache = CrossMarketEventCache()
    return _cache


def reset_cache():
    """重置单例 (用于测试)"""
    global _cache
    _cache = None
