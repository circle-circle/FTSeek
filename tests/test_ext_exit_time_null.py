"""EXT 未平仓记录 exit_time 必须为 NULL (2026-10-20 记录卫生).

病灶: `_write_open_position` INSERT 显式写 `exit_time=''`，而运维/重启查询
`WHERE exit_time IS NULL` 只看到 playbook 的 ADSK，漏掉 ASML/TSM/BA 三个 EXT 仓。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import scripts.extended_hours_entry_executor as executor


def _create_schema(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE open_positions ("
        " position_id TEXT PRIMARY KEY, symbol TEXT, direction TEXT, size REAL,"
        " avg_entry_price REAL, entry_time TEXT, strategy_id TEXT, source TEXT,"
        " owner_strategy TEXT, owner TEXT, mode TEXT, account_type TEXT,"
        " trader_type TEXT, is_filled INTEGER, version INTEGER, updated_at TEXT,"
        " metadata_json TEXT, sync_status TEXT, initial_sl REAL, current_sl REAL,"
        " stop_loss_price REAL)"
    )
    conn.execute(
        "CREATE TABLE trade_records ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, trade_date TEXT, symbol TEXT,"
        " direction TEXT, entry_time TEXT, entry_price REAL, quantity INTEGER,"
        " source_type TEXT, trade_mode TEXT, mode TEXT, owner_strategy TEXT,"
        " strategy_id TEXT, futu_order_id TEXT, metadata_json TEXT, created_at TEXT,"
        " exit_price REAL, exit_time TEXT, config_version TEXT, entry_session TEXT,"
        " decision_id TEXT)"
    )
    conn.commit()
    conn.close()


def test_ext_open_trade_record_uses_null_exit_time(tmp_path, monkeypatch):
    db = tmp_path / "ext.db"
    _create_schema(str(db))
    monkeypatch.setattr(executor, "DB", str(db))
    monkeypatch.setattr(
        executor, "ensure_test_symbol_not_in_production_db", lambda *a, **k: None)
    monkeypatch.setattr(
        executor, "_now_et",
        lambda: datetime(2026, 10, 19, 7, 51, tzinfo=timezone.utc))
    monkeypatch.setattr(executor, "_today_et", lambda: "2026-10-19")

    pos_id = executor._write_open_position(
        "US.TESTX", 1, 100.0, 100.0, "premarket", {}, 1.0, 1000,
        buy_order_id="TEST-ORDER-1",
    )
    assert pos_id.startswith("EXT-")

    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT exit_time IS NULL, typeof(exit_time), exit_price "
            "FROM trade_records WHERE symbol='US.TESTX'"
        ).fetchone()
        assert row == (1, "null", None), (
            "EXT 未平仓行必须 exit_time/exit_price 均为 NULL；空串/0 会漏仓或伪装已平仓"
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM open_positions WHERE symbol='US.TESTX' AND size>0"
        ).fetchone()[0] == 1
    finally:
        conn.close()
