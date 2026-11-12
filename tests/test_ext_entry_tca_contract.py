"""EXT 成交 TCA 写方契约（DEB-20261111 Batch A）。"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "scripts" / "extended_hours_entry_executor.py"


def _load_module(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("ext_exec_tca_under_test", EXT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    db_path = tmp_path / "ext_tca.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE open_positions (position_id TEXT, symbol TEXT, direction TEXT, size REAL, "
        "avg_entry_price REAL, entry_time TEXT, strategy_id TEXT, source TEXT, owner_strategy TEXT, "
        "owner TEXT, mode TEXT, account_type TEXT, trader_type TEXT, is_filled INTEGER, version INTEGER, "
        "updated_at TEXT, metadata_json TEXT, sync_status TEXT, initial_sl REAL, current_sl REAL, "
        "stop_loss_price REAL)"
    )
    conn.execute(
        "CREATE TABLE trade_records (id INTEGER PRIMARY KEY AUTOINCREMENT, trade_date TEXT, symbol TEXT, "
        "direction TEXT, entry_time TEXT, entry_price REAL, quantity REAL, source_type TEXT, trade_mode TEXT, "
        "mode TEXT, owner_strategy TEXT, strategy_id TEXT, futu_order_id TEXT, metadata_json TEXT, "
        "created_at TEXT, exit_price REAL, exit_time TEXT, config_version TEXT, entry_session TEXT, "
        "decision_id TEXT)"
    )
    conn.commit()
    conn.close()
    module.DB = str(db_path)
    return module, db_path


def _metadata(db_path, table):
    conn = sqlite3.connect(db_path)
    row = conn.execute(f"SELECT metadata_json FROM {table} LIMIT 1").fetchone()
    conn.close()
    return json.loads(row[0])


def test_ext_long_fill_writes_adverse_slippage_and_price_source(monkeypatch, tmp_path):
    module, db_path = _load_module(monkeypatch, tmp_path)
    module._write_open_position(
        "US.TCATST", 10, 101.0, 100.0, "premarket", {}, 1.0, 1000,
        buy_order_id="OID-LONG", grade="A", side="LONG", decision_id="EXT-LONG",
    )
    for table in ("trade_records", "open_positions"):
        metadata = _metadata(db_path, table)
        assert metadata["entry_price_source"] == "ext_broker_deal_confirm"
        assert metadata["execution"]["arrival_price"] == 100.0
        assert metadata["execution"]["fill_price"] == 101.0
        assert metadata["execution"]["slippage_bps"] == 100.0


def test_ext_short_fill_uses_adverse_positive_convention(monkeypatch, tmp_path):
    module, db_path = _load_module(monkeypatch, tmp_path)
    module._write_open_position(
        "US.TCATST", 10, 99.0, 100.0, "afterhours", {}, -1.0, 1000,
        buy_order_id="OID-SHORT", grade="B", side="SHORT", decision_id="EXT-SHORT",
    )
    metadata = _metadata(db_path, "trade_records")
    assert metadata["execution"]["slippage_bps"] == 100.0


def test_ext_missing_decision_price_keeps_slippage_null(monkeypatch, tmp_path):
    module, db_path = _load_module(monkeypatch, tmp_path)
    module._write_open_position(
        "US.TCATST", 10, 99.0, None, "premarket", {}, 1.0, 1000,
        buy_order_id="OID-NULL", grade="B", side="LONG", decision_id="EXT-NULL",
    )
    metadata = _metadata(db_path, "trade_records")
    assert metadata["execution"]["slippage_bps"] is None
    assert metadata["entry_price_source"] == "ext_broker_deal_confirm"
