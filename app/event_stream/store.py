from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterable, List, Optional

from app.event_stream.models import CanonicalEvent, RawEvent


class EventStore:
    def __init__(self, path: Optional[Path] = None):
        if path is None:
            from app.core.paths import get_data_dir
            path = get_data_dir() / "event_stream.db"
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS raw_events (
                    raw_id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    source_tier TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    source_published_at TEXT,
                    fetched_at TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    raw_artifact TEXT NOT NULL,
                    parse_status TEXT NOT NULL,
                    error TEXT
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS canonical_events (
                    event_id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    source_tier TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    event_time TEXT,
                    headline TEXT NOT NULL,
                    summary TEXT,
                    facts_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    reasoning_version TEXT NOT NULL,
                    first_seen_at TEXT,
                    published_to_seen_ms INTEGER,
                    event_meta_json TEXT
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS source_health (
                    source_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    latency_ms INTEGER,
                    last_success_at TEXT,
                    last_error TEXT
                )"""
            )
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(canonical_events)")
            }
            if "first_seen_at" not in columns:
                conn.execute("ALTER TABLE canonical_events ADD COLUMN first_seen_at TEXT")
            if "published_to_seen_ms" not in columns:
                conn.execute(
                    "ALTER TABLE canonical_events ADD COLUMN published_to_seen_ms INTEGER"
                )
            if "event_meta_json" not in columns:
                conn.execute(
                    "ALTER TABLE canonical_events ADD COLUMN event_meta_json TEXT"
                )
            # 2026-10-17 审计: 按 first_seen 时间窗取事件, 避免 "先取 500 行再过滤"
            #   导致 24h 内 53% 的事件根本没被读到.
            conn.execute(
                """CREATE INDEX IF NOT EXISTS idx_canonical_events_first_seen
                   ON canonical_events(first_seen_at)"""
            )
            missing_latency = conn.execute(
                """SELECT event_id, event_time, first_seen_at
                   FROM canonical_events
                   WHERE published_to_seen_ms IS NULL"""
            ).fetchall()
            for event_id, event_time, first_seen_at in missing_latency:
                if not first_seen_at:
                    continue
                latency_ms = self._published_to_seen_ms(event_time, first_seen_at)
                if latency_ms is not None:
                    conn.execute(
                        "UPDATE canonical_events SET published_to_seen_ms=? WHERE event_id=?",
                        (latency_ms, event_id),
                    )

    @staticmethod
    def _published_to_seen_ms(event_time: Optional[str], first_seen_at: str) -> Optional[int]:
        if not event_time:
            return None
        published = None
        try:
            published = datetime.fromisoformat(event_time.replace("Z", "+00:00"))
        except ValueError:
            try:
                published = parsedate_to_datetime(event_time)
            except (TypeError, ValueError):
                return None
        try:
            first_seen = datetime.fromisoformat(first_seen_at.replace("Z", "+00:00"))
        except ValueError:
            return None
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        if first_seen.tzinfo is None:
            first_seen = first_seen.replace(tzinfo=timezone.utc)
        latency = (first_seen - published).total_seconds() * 1000
        if latency < 0:
            return None
        return int(latency)

    def append_raw_event(self, raw: RawEvent) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO raw_events VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )""",
                (
                    raw.raw_id,
                    raw.source_id,
                    raw.source_tier,
                    raw.source_url,
                    raw.source_published_at,
                    raw.fetched_at,
                    raw.payload_hash,
                    raw.raw_artifact,
                    raw.parse_status,
                    raw.error,
                ),
            )
            return cursor.rowcount > 0

    def append_canonical_event(
        self,
        event: CanonicalEvent,
        first_seen_at: Optional[str] = None,
    ) -> bool:
        first_seen_at = first_seen_at or datetime.now(timezone.utc).isoformat()
        published_to_seen_ms = self._published_to_seen_ms(event.event_time, first_seen_at)
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO canonical_events VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )""",
                (
                    event.event_id,
                    event.source_id,
                    event.source_tier,
                    event.source_url,
                    event.event_type,
                    event.event_time,
                    event.headline,
                    event.summary,
                    json.dumps(event.facts, ensure_ascii=False, sort_keys=True),
                    event.status,
                    event.confidence,
                    event.reasoning_version,
                    first_seen_at,
                    published_to_seen_ms,
                    json.dumps(event.event_meta or {}, ensure_ascii=False, sort_keys=True),
                ),
            )
            return cursor.rowcount > 0

    def get_recent_canonical_events(self, limit: int = 50) -> List[CanonicalEvent]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT event_id, source_id, source_tier, source_url, event_type,
                          event_time, headline, summary, facts_json, status, confidence,
                          reasoning_version, event_meta_json
                   FROM canonical_events
                   ORDER BY rowid DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
        return [self._event_from_row(row) for row in rows]

    def get_canonical_events_since(
        self,
        since_iso: str,
        limit: Optional[int] = None,
    ) -> List[CanonicalEvent]:
        """按 first_seen_at 时间窗取事件 (含边界), 不再先用 rowid 截断。

        first_seen_at 由采集侧统一写成 UTC ISO 字符串, 可直接做字典序比较;
        event_time 是混合格式 (RFC822/纯日期/带时区 ISO), 不参与 SQL 过滤。
        """
        sql = """SELECT event_id, source_id, source_tier, source_url, event_type,
                        event_time, headline, summary, facts_json, status, confidence,
                        reasoning_version, event_meta_json
                 FROM canonical_events
                 WHERE first_seen_at IS NOT NULL AND first_seen_at >= ?
                 ORDER BY rowid DESC"""
        params: List[object] = [since_iso]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._event_from_row(row) for row in rows]

    @staticmethod
    def _event_from_row(row) -> CanonicalEvent:
        try:
            event_meta = json.loads(row[12]) if row[12] else {}
        except (TypeError, ValueError):
            event_meta = {}
        return CanonicalEvent(
            event_id=row[0],
            source_id=row[1],
            source_tier=row[2],
            source_url=row[3],
            event_type=row[4],
            event_time=row[5],
            headline=row[6],
            summary=row[7] or "",
            facts=json.loads(row[8]),
            status=row[9],
            confidence=row[10],
            reasoning_version=row[11],
            event_meta=event_meta if isinstance(event_meta, dict) else {},
        )

    def mark_source_health(
        self,
        source_id: str,
        status: str,
        latency_ms: int,
        last_success_at: Optional[str] = None,
        last_error: Optional[str] = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO source_health (source_id, status, latency_ms, last_success_at, last_error)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(source_id) DO UPDATE SET
                       status=excluded.status,
                       latency_ms=excluded.latency_ms,
                       last_success_at=COALESCE(excluded.last_success_at, source_health.last_success_at),
                       last_error=excluded.last_error""",
                (source_id, status, latency_ms, last_success_at, last_error),
            )

    def get_source_health(self, source_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT source_id, status, latency_ms, last_success_at, last_error
                   FROM source_health WHERE source_id=?""",
                (source_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "source_id": row[0],
            "status": row[1],
            "latency_ms": row[2],
            "last_success_at": row[3],
            "last_error": row[4],
        }

    def get_counts(self) -> dict:
        with self._connect() as conn:
            raw_events = conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0]
            canonical_events = conn.execute(
                "SELECT COUNT(*) FROM canonical_events"
            ).fetchone()[0]
            source_health = conn.execute("SELECT COUNT(*) FROM source_health").fetchone()[0]
        return {
            "raw_events": raw_events,
            "canonical_events": canonical_events,
            "source_health": source_health,
        }

    def get_source_health_rows(self) -> List[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT source_id, status, latency_ms, last_success_at, last_error
                   FROM source_health ORDER BY source_id"""
            ).fetchall()
        return [
            {
                "source_id": row[0],
                "status": row[1],
                "latency_ms": row[2],
                "last_success_at": row[3],
                "last_error": row[4],
            }
            for row in rows
        ]

    def get_latency_stats(self) -> List[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT source_id,
                          COUNT(published_to_seen_ms),
                          AVG(published_to_seen_ms),
                          MIN(published_to_seen_ms),
                          MAX(published_to_seen_ms)
                   FROM canonical_events
                   WHERE published_to_seen_ms IS NOT NULL
                   GROUP BY source_id
                   ORDER BY source_id"""
            ).fetchall()
        return [
            {
                "source_id": row[0],
                "events_with_latency": int(row[1]),
                "avg_published_to_seen_ms": round(float(row[2])) if row[2] is not None else None,
                "min_published_to_seen_ms": int(row[3]) if row[3] is not None else None,
                "max_published_to_seen_ms": int(row[4]) if row[4] is not None else None,
            }
            for row in rows
        ]
