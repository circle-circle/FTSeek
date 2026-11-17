"""RFC-211 A1: 入场记账 Outbox (开关默认关 + fail-safe + 幂等 + 干净重放) 契约测试。"""
import json
import sqlite3

import pytest

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


def _add_filled_instruction(pdb, instruction_id=10, attempt=1, order_id="oid1"):
    conn = pdb._conn()
    conn.execute("""CREATE TABLE playbook_instructions (
        id INTEGER PRIMARY KEY, execution_state TEXT, execution_attempt INTEGER,
        broker_order_id TEXT, metadata_json TEXT, executed INTEGER,
        execution_price REAL, execution_time TEXT, outcome TEXT,
        skip_reason TEXT, updated_at TEXT)""")
    metadata = {"entry_fill": {"confirmed": 1, "attempt": attempt, "order_id": order_id}}
    conn.execute(
        "INSERT INTO playbook_instructions (id, execution_state, execution_attempt, "
        "broker_order_id, metadata_json, executed) VALUES (?,?,?,?,?,0)",
        (instruction_id, "FILLED_UNBOOKED", attempt, order_id, json.dumps(metadata)))
    conn.commit()


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


def test_mark_booked_and_consume_intent_are_atomic():
    pdb = _outbox_db()
    _add_filled_instruction(pdb)
    pdb.persist_booking_intent(10, 1, '{"v":1}', "oid1", "sim", "US.X")
    assert pdb.mark_execution_booked(10, 1, execution_price=100.0) is True
    row = pdb._conn().execute(
        "SELECT execution_state FROM playbook_instructions WHERE id=10").fetchone()
    assert row[0] == "BOOKED"
    assert pdb.get_pending_booking_intent(10) is None


def test_mark_booked_rolls_back_when_consume_fails():
    pdb = _outbox_db()
    _add_filled_instruction(pdb)
    pdb.persist_booking_intent(10, 1, '{"v":1}', "oid1", "sim", "US.X")
    pdb._conn().execute(
        "CREATE TRIGGER reject_outbox_consume BEFORE UPDATE ON entry_booking_outbox "
        "BEGIN SELECT RAISE(ABORT, 'forced consume failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="forced consume failure"):
        pdb.mark_execution_booked(10, 1)
    row = pdb._conn().execute(
        "SELECT execution_state FROM playbook_instructions WHERE id=10").fetchone()
    assert row[0] == "FILLED_UNBOOKED"
    assert pdb.get_pending_booking_intent(10) is not None


def test_mark_booked_without_outbox_table_keeps_legacy_schema_compatible():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    pdb = PlaybookDB(conn=conn)
    _add_filled_instruction(pdb)
    assert pdb.mark_execution_booked(10, 1) is True


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


def test_pending_lookup_is_scoped_to_current_order():
    pdb = _outbox_db()
    pdb.persist_booking_intent(10, 1, '{"v":1}', "oid1", "sim", "US.X")
    assert pdb.get_pending_booking_intent(10, futu_order_id="oid2") is None
    assert pdb.get_pending_booking_intent(10, futu_order_id="oid1")["attempt"] == 1


def test_outbox_switch_is_sim_only(monkeypatch):
    monkeypatch.setattr(pconfig, "get_playbook_config", lambda: _FakeConfig(True))
    assert pconfig.entry_booking_outbox_enabled("sim") is True
    assert pconfig.entry_booking_outbox_enabled("simulate") is True
    assert pconfig.entry_booking_outbox_enabled("real") is False
    assert pconfig.entry_booking_outbox_enabled("live") is False


def test_replay_real_is_inert_even_when_global_switch_on(tmp_path, monkeypatch):
    db_path, pdb = _setup_replay(tmp_path, monkeypatch, enabled=True)
    ok = _inst()._replay_pending_booking_intent(10, db_path, "real", "oid1", "US.X")
    assert ok is False
    assert pdb.get_pending_booking_intent(10) is not None
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM trade_records").fetchone()[0] == 0
    conn.close()


def test_replay_never_uses_intent_from_another_order(tmp_path, monkeypatch):
    db_path, pdb = _setup_replay(tmp_path, monkeypatch, enabled=True)
    ok = _inst()._replay_pending_booking_intent(10, db_path, "sim", "oid2", "US.X")
    assert ok is False
    assert pdb.get_pending_booking_intent(10) is not None
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM trade_records").fetchone()[0] == 0
    conn.close()


def test_backfill_existing_trade_consumes_and_marks_replay(tmp_path, monkeypatch):
    db_path, pdb = _setup_replay(tmp_path, monkeypatch, enabled=True, existing_trade=True)
    monkeypatch.setattr(ps, "_get_db_path", lambda: db_path)
    ok = _inst()._backfill_playbook_trade_record(
        symbol="US.X", direction="LONG", qty=10, entry=100.0, sl=95.0, tp=110.0,
        trace_id="existing-replay", futu_order_id="oid1", broker_env="sim",
        order_evidence={"confirmed": True, "order_id": "oid1", "dealt_qty": 10, "price": 100.0},
        broker_position={"qty": 10, "direction": "LONG"}, instruction_id=10)
    assert ok is True
    assert pdb.get_pending_booking_intent(10) is None
    conn = sqlite3.connect(db_path)
    count, meta_raw = conn.execute("SELECT COUNT(*), metadata_json FROM trade_records").fetchone()
    conn.close()
    assert count == 1
    assert json.loads(meta_raw)["booking_outbox_replay"] is True


def test_replay_existing_conflict_is_not_consumed(tmp_path, monkeypatch):
    db_path, pdb = _setup_replay(tmp_path, monkeypatch, enabled=True, existing_trade=True)
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE trade_records SET quantity=11 WHERE futu_order_id='oid1'")
    conn.commit()
    conn.close()
    ok = _inst()._replay_pending_booking_intent(10, db_path, "sim", "oid1", "US.X")
    assert ok is False
    assert pdb.get_pending_booking_intent(10) is not None


def test_playbook_remark_restores_only_exact_instruction_identity():
    assert ps._playbook_instruction_id_from_remark("pb_77_sim_2") == 77
    assert ps._playbook_instruction_id_from_remark("pb_78_real_3") == 78
    assert ps._playbook_instruction_id_from_remark("ext_entry_77") is None
    assert ps._playbook_instruction_id_from_remark("pb_77_sim") is None
    assert ps._playbook_instruction_id_from_remark("pb_77_sim_2_extra") is None
