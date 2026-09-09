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
import os
import sqlite3
import sys
import threading
import time
from contextlib import closing
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_lock = threading.Lock()


def _is_test_context() -> bool:
    """pytest 会话内调用 (测试流量标记, 2026-08-27): 测试无数据目录隔离,
    历史上 fallback/cache/simple/test_xyz 四个测试 feature 已写 ~3.7k 行进
    生产审计库 (占 72%), 污染 token 日报与 intraday_heartbeat 的 LLM 存活
    检测 (30min 窗口内测试行可掩盖真实断流). 生产进程 (main.py) 不满足任一条件.

    FTSEEK_SHADOW_RUN=1 (2026-09-09 F2): 脚本级影子实验 (thinking_eval 等) 经网关的
    调用同样打 is_test=1 — 是真实付费调用但非生产流量; 成本由日报影子段从各自
    jsonl 独立归集, 生产存活检测/网关口径不受污染。"""
    try:
        return bool(os.environ.get("PYTEST_CURRENT_TEST")) or "pytest" in sys.modules \
            or os.environ.get("FTSEEK_SHADOW_RUN") == "1"
    except Exception:
        return False


def _get_db_path() -> str:
    # R-3 (2026-08-30, DEB-timezone-duplication-audit): 收敛到 db_pool 单一定义。
    # 原三处平行定义 (db_pool._DB_PATHS / db_utils.get_llm_audit_db / 本函数) 同值收敛;
    # 本函数原主路的 yaml paths.data_dir 覆写对 llm_audit 退役 (yaml 现值=默认值, 零行为变化)。
    try:
        from app.storage.db_pool import get_db_path
        return str(get_db_path("llm_audit"))
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
            created_at      TEXT DEFAULT CURRENT_TIMESTAMP,
            is_test         INTEGER DEFAULT 0
        )
    """)
    # 旧库迁移: is_test 列 (2026-08-27 测试流量隔离)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(llm_calls)")}
    if "is_test" not in cols:
        conn.execute("ALTER TABLE llm_calls ADD COLUMN is_test INTEGER DEFAULT 0")
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
            # DEB-20260903 #57: 用 closing() 确保异常路径连接关闭 —
            # 原 conn.close() 不在 finally, _ensure_schema/execute/commit 任一步
            # 抛异常时 SQLite 连接句柄泄漏
            with closing(sqlite3.connect(_get_db_path())) as conn:
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
                    " success, cost_usd, latency_ms, tokens_in, tokens_out, result_summary, error_msg, is_test) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                        1 if _is_test_context() else 0,
                    ),
                )
                conn.commit()
    except Exception as e:
        logger.debug(f"[LLM Audit] 写入失败: {e}")


def get_feature_cost_today(feature: str) -> float:
    """该 feature 当日累计成本 (USD, 仅生产流量)"""
    try:
        with _lock:
            with closing(sqlite3.connect(_get_db_path())) as conn:
                _ensure_schema(conn)
                cursor = conn.execute(
                    "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_calls "
                    "WHERE feature = ? AND COALESCE(is_test, 0) = 0 "
                    "AND date(timestamp,'unixepoch','localtime')=date('now','localtime')",
                    (feature,),
                )
                row = cursor.fetchone()
                return float(row[0] or 0)
    except Exception as e:
        logger.debug(f"[LLM Audit] 查询失败: {e}")
        return 0.0


def get_feature_call_count(feature: str, hours: int = 24) -> int:
    try:
        with _lock:
            with closing(sqlite3.connect(_get_db_path())) as conn:
                _ensure_schema(conn)
                cursor = conn.execute(
                    "SELECT COUNT(*) FROM llm_calls "
                    "WHERE feature = ? AND COALESCE(is_test, 0) = 0 AND timestamp > ?",
                    (feature, time.time() - hours * 3600),
                )
                row = cursor.fetchone()
                return int(row[0] or 0)
    except Exception:
        return 0
