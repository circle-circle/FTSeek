"""event_stream 保留窗治理的契约测试 (2026-10-15).

钉死: ①老行 artifact 置 NULL 但行保留 (dedup 基座不动) ②新行不动
③canonical_events 不动 ④dry-run 零写入。
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import scripts.event_stream_retention as ret


def _mk_db(path):
    conn = sqlite3.connect(str(path))
    conn.execute("""CREATE TABLE raw_events (
        raw_id TEXT PRIMARY KEY, source_id TEXT, source_tier TEXT,
        source_url TEXT, source_published_at TEXT, fetched_at TEXT,
        payload_hash TEXT, raw_artifact TEXT, parse_status TEXT, error TEXT)""")
    conn.execute("""CREATE TABLE canonical_events (
        event_id TEXT PRIMARY KEY, payload TEXT)""")
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    new = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    conn.execute("INSERT INTO raw_events VALUES ('r-old','S','t','u','p',?, 'h','BODY_OLD','ok','')", (old,))
    conn.execute("INSERT INTO raw_events VALUES ('r-new','S','t','u','p',?, 'h','BODY_NEW','ok','')", (new,))
    conn.execute("INSERT INTO raw_events VALUES ('r-null','S','t','u','p',?, 'h',NULL,'ok','')", (old,))
    conn.execute("INSERT INTO canonical_events VALUES ('c1','keepme')")
    conn.commit()
    conn.close()


def test_old_artifact_nulled_rows_kept(tmp_path, monkeypatch):
    db = tmp_path / "es.db"
    _mk_db(db)
    monkeypatch.setattr(ret, "DB", db)
    out = ret.clean(days=14)
    assert out["rows_to_null"] == 1 and out["nulled"] == 1
    conn = sqlite3.connect(str(db))
    rows = dict(conn.execute("SELECT raw_id, raw_artifact FROM raw_events").fetchall())
    assert rows["r-old"] is None            # 老行清掉
    assert rows["r-new"] == "BODY_NEW"      # 新行不动
    assert "r-null" in rows                 # 本就 NULL 的行还在
    assert len(rows) == 3                   # 行全保留 (dedup 基座)
    assert conn.execute("SELECT payload FROM canonical_events").fetchone()[0] == "keepme"
    conn.close()


def test_dry_run_writes_nothing(tmp_path, monkeypatch):
    db = tmp_path / "es.db"
    _mk_db(db)
    monkeypatch.setattr(ret, "DB", db)
    out = ret.clean(days=14, dry_run=True)
    assert out["rows_to_null"] == 1 and "nulled" not in out
    conn = sqlite3.connect(str(db))
    assert conn.execute(
        "SELECT raw_artifact FROM raw_events WHERE raw_id='r-old'").fetchone()[0] == "BODY_OLD"
    conn.close()
