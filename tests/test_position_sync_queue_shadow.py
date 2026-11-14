"""PositionSync 补录路径的 RFC-203 queue 影子契约。"""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

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
