"""
LLM Feature Audit DB (L0 2026-05-14)

每次 LLM 调用 (不论 feature) 落 1 行: feature/model/prompt_hash/response_hash/cost/latency_ms/success/result_summary.
便于:
- 月度成本归因
- 各 feature 命中率统计
- 调用频次审计
"""
from __future__ import annotations
import hashlib
import json
import logging
import sqlite3
import threading
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_lock = threading.Lock()


def _get_db_path() -> str:
    try:
        from app.core.paths import get_data_dir
        return str(get_data_dir() / "llm_audit.db")
    except Exception:
        from pathlib import Path
        return str(Path(__file__).resolve().parent.parent.parent.parent / "data" / "llm_audit.db")


def _ensure_schema(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS llm_calls (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            feature         TEXT NOT NULL,
            model           TEXT,
            timestamp       REAL NOT NULL,
            prompt_hash     TEXT,
            prompt_len      INTEGER,
            response_hash   TEXT,
            response_len    INTEGER,
            success         INTEGER,
            cost_usd        REAL,
            latency_ms      REAL,
            tokens_in       INTEGER,
            tokens_out      INTEGER,
            result_summary  TEXT,
            error_msg       TEXT,
            created_at      TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_feature ON llm_calls(feature)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_time ON llm_calls(timestamp)")


def record_call(
    feature: str,
    model: str,
    prompt: str,
    response: Optional[str],
    success: bool,
    cost_usd: float = 0.0,
    latency_ms: float = 0.0,
    tokens_in: int = 0,
    tokens_out: int = 0,
    result_summary: Optional[Dict[str, Any]] = None,
    error_msg: Optional[str] = None,
):
    """落 1 行 LLM 调用审计. 失败静默不阻断业务."""
    try:
        with _lock:
            conn = sqlite3.connect(_get_db_path())
            _ensure_schema(conn)
            ph = hashlib.md5((prompt or "").encode("utf-8")).hexdigest()[:12]
            rh = hashlib.md5((response or "").encode("utf-8")).hexdigest()[:12]
            summary = ""
            if result_summary:
                try:
                    summary = json.dumps(result_summary, ensure_ascii=False)[:500]
                except Exception:
                    summary = str(result_summary)[:500]
            conn.execute(
                "INSERT INTO llm_calls "
                "(feature, model, timestamp, prompt_hash, prompt_len, response_hash, response_len, "
                " success, cost_usd, latency_ms, tokens_in, tokens_out, result_summary, error_msg) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    feature, model, time.time(),
                    ph, len(prompt or ""),
                    rh, len(response or ""),
                    int(bool(success)),
                    float(cost_usd or 0),
                    float(latency_ms or 0),
                    int(tokens_in or 0),
                    int(tokens_out or 0),
                    summary,
                    (error_msg or "")[:500],
                ),
            )
            conn.commit()
            conn.close()
    except Exception as e:
        logger.debug(f"[LLM Audit] 写入失败: {e}")


def get_feature_cost_today(feature: str) -> float:
    """该 feature 当日累计成本 (USD)"""
    try:
        with _lock:
            conn = sqlite3.connect(_get_db_path())
            _ensure_schema(conn)
            cursor = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_calls "
                "WHERE feature = ? AND date(timestamp,'unixepoch','localtime')=date('now','localtime')",
                (feature,),
            )
            row = cursor.fetchone()
            conn.close()
            return float(row[0] or 0)
    except Exception as e:
        logger.debug(f"[LLM Audit] 查询失败: {e}")
        return 0.0


def get_feature_call_count(feature: str, hours: int = 24) -> int:
    try:
        with _lock:
            conn = sqlite3.connect(_get_db_path())
            _ensure_schema(conn)
            cursor = conn.execute(
                "SELECT COUNT(*) FROM llm_calls "
                "WHERE feature = ? AND timestamp > ?",
                (feature, time.time() - hours * 3600),
            )
            row = cursor.fetchone()
            conn.close()
            return int(row[0] or 0)
    except Exception:
        return 0
