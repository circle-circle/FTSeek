"""MOM 指令与其他写入方的跨策略覆盖隔离。"""
import sqlite3

import pytest

from app.playbook.db import PlaybookDB


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(
        "app.playbook.admission.apply", lambda inst: (inst, {"verdict": "PASS"}))
    monkeypatch.setattr(
        "app.playbook.admission.evaluate", lambda inst: {"verdict": "PASS"})
    monkeypatch.setattr(
        "app.playbook.conflict_shadow.emit_save_conflict_shadow", lambda *args: None)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    playbook_db = PlaybookDB(conn=conn)
    playbook_db.ensure_tables()
    yield playbook_db
    conn.close()


def _instruction(decision_id, stop_loss):
    return {
        "date": "2026-11-13", "symbol": "US.QQQ", "direction": "BUY",
        "entry_price": 100.0, "stop_loss": stop_loss, "take_profit": 0,
        "position_size": 40, "confidence": 70, "decision_id": decision_id,
        "thesis": "contract",
    }


def test_da_cannot_take_over_unexecuted_mom_instruction(db):
    mom_id = db.save_instruction(_instruction("MOM-ETF-V1-20261113-US.QQQ", 97.0))
    returned = db.save_instruction(_instruction("DA-20261113-US.QQQ", 95.0))

    assert returned == mom_id
    rows = db._conn().execute(
        "SELECT id,decision_id,stop_loss,position_size FROM playbook_instructions"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["id"] == mom_id
    assert rows[0]["decision_id"] == "MOM-ETF-V1-20261113-US.QQQ"
    assert rows[0]["stop_loss"] == 97.0
    assert rows[0]["position_size"] == 40


def test_mom_cannot_take_over_existing_da_instruction(db):
    da_id = db.save_instruction(_instruction("DA-20261113-US.QQQ", 95.0))
    returned = db.save_instruction(_instruction("MOM-ETF-V1-20261113-US.QQQ", 97.0))

    assert returned == da_id
    rows = db._conn().execute(
        "SELECT id,decision_id,stop_loss FROM playbook_instructions"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["id"] == da_id
    assert rows[0]["decision_id"] == "DA-20261113-US.QQQ"
    assert rows[0]["stop_loss"] == 95.0


def test_mom_same_decision_id_remains_idempotent(db):
    first = db.save_instruction(_instruction("MOM-ETF-V1-20261113-US.QQQ", 97.0))
    second = db.save_instruction(_instruction("MOM-ETF-V1-20261113-US.QQQ", 96.5))
    assert first == second
    assert db._conn().execute("SELECT COUNT(*) FROM playbook_instructions").fetchone()[0] == 1
