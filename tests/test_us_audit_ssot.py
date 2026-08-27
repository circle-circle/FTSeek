"""us_audit 盘后巡检持仓检查 SSOT 契约测试 (2026-08-27 修复).

原缺陷: run_post_market_audit 读内存 position_store — 巡检在进程重启后
闭市段首跑 (8/27 实录: SSOT 注入 3 只后 6s 巡检仍报"当前持仓数: 0"),
内存 store 尚未 broker_sync 必为空, 持仓检查结构性空转.
修复: 改读 open_positions SSOT (与 main.py 启动注入同源).
"""
import sqlite3

import pytest

from app.harness.us_audit import run_post_market_audit


class _MemStore:
    """内存 PositionStore 替身 — 模拟启动初期: 有 store 但为空."""

    def all(self):
        return []


class _FakeScheduler:
    position_store = _MemStore()


@pytest.fixture
def ssot_db(tmp_path, monkeypatch):
    db = tmp_path / "positions.db"
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE open_positions (
            symbol TEXT PRIMARY KEY, direction TEXT, size REAL,
            avg_entry_price REAL, entry_time TEXT, updated_at TEXT
        )
    """)
    conn.executemany(
        "INSERT INTO open_positions (symbol, direction, size, avg_entry_price, "
        "entry_time, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("US.MU", "LONG", 6.0, 946.87, "2026-08-27T09:38:22-04:00", "x"),
            ("US.AVGO", "SHORT", 16.0, 363.55, "2026-08-27T09:48:34-04:00", "x"),
            ("US.PDD", "LONG", 69.0, 85.27, "2026-08-27T09:53:24-04:00", "x"),
            ("US.ORCL", "LONG", 38.0, 151.6, "2026-08-27T09:53:33-04:00", "x"),
            ("US.MRK", "LONG", 39.0, 150.135, "2026-08-27T09:53:36-04:00", "x"),
            ("US.BSX", "SHORT", 123.0, 47.73, "2026-08-27T09:59:27-04:00", "x"),
        ])
    conn.commit()
    conn.close()
    monkeypatch.setattr(
        "app.core.position_sync._get_db_path", lambda: str(db))
    return db


def test_post_market_audit_reads_ssot_not_empty_memory(ssot_db):
    """内存 store 为空 (启动初期) 时, 持仓数必须来自 SSOT 而非报 0."""
    result = run_post_market_audit(_FakeScheduler())
    assert result["open_positions"] == 6


def test_post_market_audit_ssot_empty_reports_zero(ssot_db):
    conn = sqlite3.connect(ssot_db)
    conn.execute("DELETE FROM open_positions")
    conn.commit()
    conn.close()
    result = run_post_market_audit(_FakeScheduler())
    assert result["open_positions"] == 0


def test_post_market_audit_us_only_filter(ssot_db):
    """非 US 前缀行 (如残留 HK) 不计入 US 巡检持仓数."""
    conn = sqlite3.connect(ssot_db)
    conn.execute(
        "INSERT INTO open_positions (symbol, direction, size, avg_entry_price, "
        "entry_time, updated_at) VALUES ('HK.00700', 'LONG', 100, 300.0, 't', 'x')")
    conn.commit()
    conn.close()
    result = run_post_market_audit(_FakeScheduler())
    assert result["open_positions"] == 6
