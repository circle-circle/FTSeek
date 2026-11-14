"""RFC-211 A1: 入场记账 Outbox (开关默认关 + fail-safe + 幂等 + 干净重放) 契约测试。"""
import json
import sqlite3

import app.core.position_sync as ps
import app.playbook.config as pconfig
import app.playbook.db as pdb_mod
from app.playbook.db import PlaybookDB
from app.storage.trade_store import TradeStore as RealTradeStore

_OUTBOX_SCHEMA = """CREATE TABLE entry_booking_outbox (
    intent_id TEXT PRIMARY KEY, instruction_id INTEGER, attempt INTEGER,
    target_env TEXT, symbol TEXT, futu_order_id TEXT, intent_json TEXT,
    state TEXT DEFAULT 'PENDING', version INTEGER DEFAULT 0,
    created_at TEXT, updated_at TEXT, UNIQUE(instruction_id, attempt))"""


def _outbox_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(_OUTBOX_SCHEMA)
    return PlaybookDB(conn=conn)


def _record(regime="cautious", atr=4.0, oid="oid1"):
    return {
        "trace_id": "t1", "trade_date": "2026-11-10", "symbol": "US.X", "direction": "LONG",
        "entry_time": "2026-11-10T09:41:55-04:00", "exit_time": "", "entry_price": 100.0,
        "exit_price": 0, "quantity": 10, "realized_pnl": 0, "pnl_pct": 0, "exit_reason": "",
        "holding_hours": 0, "signal_strength": 50, "signal_confidence": 0.5, "futu_order_id": oid,
        "decision_id": "DA-x", "entry_session": "premarket", "entry_vix": 15.0,
        "source_type": "PLAYBOOK_SIM", "entry_reason": "playbook_sim", "owner_strategy": "playbook_sim",
        "strategy_id": "playbook_sim", "market_regime": regime, "mode": "normal",
        "trade_mode": "simulate", "signal_id": "t1",
        "metadata_json": json.dumps({"atr": atr, "playbook_instruction_id": 10, "peak_price": 100.0}),
    }


class _FakeConfig:
    def __init__(self, enabled):
        self._enabled = enabled

    def get(self, key, default=None):
        if key == "entry_booking_outbox_enabled":
            return self._enabled
        return default


def _inst():
    return object.__new__(ps.USPositionSync)


def test_db_persist_get_consume_roundtrip():
    pdb = _outbox_db()
    pdb.persist_booking_intent(10, 1, '{"record":{"symbol":"US.X"}}', "oid1", "sim", "US.X")
    row = pdb.get_pending_booking_intent(10)
    assert row is not None and row["attempt"] == 1 and row["futu_order_id"] == "oid1"
    pdb.consume_booking_intent(10, 1)
    assert pdb.get_pending_booking_intent(10) is None


def test_db_persist_idempotent_no_overwrite():
    pdb = _outbox_db()
    pdb.persist_booking_intent(10, 1, '{"v":1}', "oid1", "sim", "US.X")
    pdb.persist_booking_intent(10, 1, '{"v":2}', "oid1", "sim", "US.X")
    row = pdb.get_pending_booking_intent(10)
    assert json.loads(row["intent_json"])["v"] == 1


def _setup_replay(tmp_path, monkeypatch, enabled, with_intent=True, existing_trade=False):
    db_path = str(tmp_path / "trades.db")
    RealTradeStore(db_path)
    pdb = _outbox_db()
    if with_intent:
        pdb.persist_booking_intent(
            10, 1, json.dumps({"record": _record(), "voting_data": None}), "oid1", "sim", "US.X")
    if existing_trade:
        RealTradeStore(db_path).save_trade(_record())
    monkeypatch.setattr(pconfig, "get_playbook_config", lambda: _FakeConfig(enabled))
    monkeypatch.setattr(pdb_mod, "route_playbook_db", lambda env: pdb)
    return db_path, pdb


def test_replay_switch_off_is_inert(tmp_path, monkeypatch):
    db_path, pdb = _setup_replay(tmp_path, monkeypatch, enabled=False)
    ok = _inst()._replay_pending_booking_intent(10, db_path, "sim", "oid1", "US.X")
    assert ok is False
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM trade_records").fetchone()[0] == 0
    conn.close()
    assert pdb.get_pending_booking_intent(10) is not None


def test_replay_no_intent_falls_through(tmp_path, monkeypatch):
    db_path, _ = _setup_replay(tmp_path, monkeypatch, enabled=True, with_intent=False)
    ok = _inst()._replay_pending_booking_intent(10, db_path, "sim", "oid1", "US.X")
    assert ok is False


def test_replay_clean_full_intent(tmp_path, monkeypatch):
    db_path, pdb = _setup_replay(tmp_path, monkeypatch, enabled=True)
    ok = _inst()._replay_pending_booking_intent(10, db_path, "sim", "oid1", "US.X")
    assert ok is True
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT market_regime, metadata_json FROM trade_records").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0]["market_regime"] == "cautious"
    meta = json.loads(rows[0]["metadata_json"])
    assert meta["atr"] == 4.0
    assert meta["booking_outbox_replay"] is True
    assert pdb.get_pending_booking_intent(10) is None


def test_replay_idempotent_no_double_book(tmp_path, monkeypatch):
    db_path, pdb = _setup_replay(tmp_path, monkeypatch, enabled=True, existing_trade=True)
    ok = _inst()._replay_pending_booking_intent(10, db_path, "sim", "oid1", "US.X")
    assert ok is True
    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM trade_records").fetchone()[0]
    conn.close()
    assert count == 1
    assert pdb.get_pending_booking_intent(10) is None


def test_replay_no_instruction_id_falls_through(tmp_path, monkeypatch):
    db_path, _ = _setup_replay(tmp_path, monkeypatch, enabled=True)
    ok = _inst()._replay_pending_booking_intent(None, db_path, "sim", "oid1", "US.X")
    assert ok is False
