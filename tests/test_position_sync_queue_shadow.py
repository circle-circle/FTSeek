"""PositionSync 补录路径的 RFC-203 queue 影子契约。"""
import json
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import app.playbook.early_risk_shadow as early_risk_shadow
from app.core.position_sync import _record_recovered_queue_candidate


def _instruction(confirmed_at="2026-11-13T09:48:12.580131-05:00"):
    return {
        "id": 5610, "symbol": "US.MU", "direction": "BUY",
        "decision_id": "DA-20261113211940-US.MU-7963",
        "metadata_json": json.dumps({
            "entry_confirm": {
                "confirmed_at": confirmed_at,
                "delay_actual_sec": 120.0,
            }
        }),
    }


def test_recovered_fill_uses_broker_time_not_reconcile_time(tmp_path):
    target = tmp_path / "queue.jsonl"
    snapshot = {
        "updated_time": "2026-11-13 09:49:03",
        "create_time": "2026-11-13 09:48:51",
    }
    now = datetime(2026, 11, 13, 9, 49, 57, tzinfo=ZoneInfo("America/New_York"))

    assert _record_recovered_queue_candidate(
        instruction=_instruction(), order_snapshot=snapshot,
        now_et_value=now, shadow_path=target) is True

    row = json.loads(target.read_text().strip())
    assert row["inst_id"] == 5610
    assert row["execution_time"] == "2026-11-13 09:49:03"
    assert row["execution_time_source"] == "broker_order_updated_or_create_time"
    assert row["latency_sec"] == 50.42
    assert row["arm_delay_actual_sec"] == 120.0


def test_recovered_fill_without_broker_timestamp_does_not_fabricate_sample(tmp_path):
    target = tmp_path / "queue.jsonl"
    now = datetime(2026, 11, 13, 9, 49, 57, tzinfo=ZoneInfo("America/New_York"))
    assert _record_recovered_queue_candidate(
        instruction=_instruction(), order_snapshot={"updated_time": "NaT"},
        now_et_value=now, shadow_path=target) is False
    assert not target.exists()


def test_recovered_fill_without_confirmation_is_not_a_queue_candidate(tmp_path):
    target = tmp_path / "queue.jsonl"
    now = datetime(2026, 11, 13, 9, 49, 57, tzinfo=ZoneInfo("America/New_York"))
    assert _record_recovered_queue_candidate(
        instruction=_instruction(confirmed_at=None),
        order_snapshot={"updated_time": "2026-11-13 09:49:03"},
        now_et_value=now, shadow_path=target) is False
    assert not target.exists()


def _sqlite_row_instruction(inst_id=5611):
    instruction = _instruction()
    instruction["id"] = inst_id
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE playbook_instructions (id INTEGER, symbol TEXT, direction TEXT,"
        " decision_id TEXT, metadata_json TEXT)")
    conn.execute(
        "INSERT INTO playbook_instructions VALUES (:id, :symbol, :direction,"
        " :decision_id, :metadata_json)", instruction)
    row = conn.execute("SELECT * FROM playbook_instructions").fetchone()
    conn.close()
    return row


def test_recovered_fill_accepts_sqlite_row_from_reconcile_path(tmp_path):
    target = tmp_path / "queue.jsonl"
    snapshot = {
        "updated_time": "2026-11-13 09:49:03",
        "create_time": "2026-11-13 09:48:51",
    }
    now = datetime(2026, 11, 13, 9, 49, 57, tzinfo=ZoneInfo("America/New_York"))

    assert _record_recovered_queue_candidate(
        instruction=_sqlite_row_instruction(), order_snapshot=snapshot,
        now_et_value=now, shadow_path=target) is True

    row = json.loads(target.read_text().strip())
    assert row["inst_id"] == 5611
    assert row["execution_time"] == "2026-11-13 09:49:03"
    assert row["execution_time_source"] == "broker_order_updated_or_create_time"
    assert row["latency_sec"] == 50.42
    assert row["arm_delay_actual_sec"] == 120.0


def test_recovered_fill_row_writer_failure_does_not_raise_secondary_error(tmp_path, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("shadow writer down")

    monkeypatch.setattr(early_risk_shadow, "record_queue_candidate", boom)
    now = datetime(2026, 11, 13, 9, 49, 57, tzinfo=ZoneInfo("America/New_York"))
    assert _record_recovered_queue_candidate(
        instruction=_sqlite_row_instruction(inst_id=5612),
        order_snapshot={"updated_time": "2026-11-13 09:49:03"},
        now_et_value=now, shadow_path=tmp_path / "queue.jsonl") is False
