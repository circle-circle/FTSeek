"""llm_audit 测试流量隔离契约测试 (2026-08-27).

背景: 测试无数据目录隔离, fallback/cache/simple/test_xyz 四个测试 feature
累计 ~3.7k 行写进生产审计库 (占 72%), 污染 token 日报与 intraday_heartbeat
的 LLM 存活检测. 修复: is_test 列 + pytest 上下文自动标记 + 读者过滤.
"""
import sqlite3

import pytest

import app.llm.features.audit_db as audit_db


@pytest.fixture
def tmp_audit_db(tmp_path, monkeypatch):
    db = tmp_path / "llm_audit.db"
    monkeypatch.setattr(audit_db, "_get_db_path", lambda: str(db))
    return db


def _rows(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT feature, is_test, cost_usd FROM llm_calls")]
    conn.close()
    return rows


def test_record_under_pytest_marks_is_test(tmp_audit_db):
    """pytest 会话内的调用自动落 is_test=1 (本测试自身即运行在 pytest 中)."""
    assert audit_db._is_test_context() is True
    audit_db.record_call("cache_feature", "qwen", "p", "r", True, cost_usd=0.01)
    rows = _rows(tmp_audit_db)
    assert len(rows) == 1 and rows[0]["is_test"] == 1


def test_record_production_context_marks_zero(tmp_audit_db, monkeypatch):
    monkeypatch.setattr(audit_db, "_is_test_context", lambda: False)
    audit_db.record_call("universe_expansion_reasoning", "qwen3.8-flash",
                         "p", "r", True, cost_usd=0.05)
    rows = _rows(tmp_audit_db)
    assert len(rows) == 1 and rows[0]["is_test"] == 0


def test_readers_exclude_test_rows(tmp_audit_db, monkeypatch):
    """成本/计数读者只看生产行 — is_test=1 的行不计入预算口径."""
    monkeypatch.setattr(audit_db, "_is_test_context", lambda: False)
    audit_db.record_call("pre_entry_gate", "qwen", "p1", "r1", True, cost_usd=0.10)
    monkeypatch.setattr(audit_db, "_is_test_context", lambda: True)
    audit_db.record_call("pre_entry_gate", "qwen", "p2", "r2", True, cost_usd=9.99)
    assert audit_db.get_feature_cost_today("pre_entry_gate") == pytest.approx(0.10)
    assert audit_db.get_feature_call_count("pre_entry_gate", hours=1) == 1


def test_old_schema_migrates_is_test_column(tmp_path):
    """旧库 (无 is_test 列) 经 _ensure_schema 自动补列, 不丢历史行."""
    db = tmp_path / "old_llm_audit.db"
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE llm_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            feature TEXT NOT NULL, model TEXT, timestamp REAL NOT NULL,
            prompt_hash TEXT, prompt_len INTEGER, response_hash TEXT,
            response_len INTEGER, success INTEGER, cost_usd REAL,
            latency_ms REAL, tokens_in INTEGER, tokens_out INTEGER,
            result_summary TEXT, error_msg TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute(
        "INSERT INTO llm_calls (feature, model, timestamp, success) "
        "VALUES ('thesis_inferer', 'qwen3.7-max', 1.0, 1)")
    conn.commit()
    conn.close()

    conn = sqlite3.connect(db)
    audit_db._ensure_schema(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(llm_calls)")}
    n = conn.execute("SELECT COUNT(*), COALESCE(MAX(is_test), 0) FROM llm_calls").fetchone()
    conn.commit()
    conn.close()
    assert "is_test" in cols
    assert n[0] == 1 and n[1] == 0  # 历史行保留, is_test 缺省 0
