"""Contract tests for the record-only instruction conflict shadow."""

import json

import pytest

import app.playbook.db as playbook_db
from app.playbook.conflict_shadow import (
    append_event,
    build_save_conflict_event,
)


def _instruction(**overrides):
    payload = {
        "date": "2026-10-01",
        "symbol": "US.TEST",
        "direction": "BUY",
        "entry_price": 100.0,
        "stop_loss": 95.0,
        "take_profit": 110.0,
        "confidence": 70,
        "skip_reason": "",
        "decision_id": "DA-20261001-US.TEST-test",
    }
    payload.update(overrides)
    return payload


def _row(**overrides):
    row = {
        "id": 1,
        "decision_id": "DA-20261001-US.TEST-test",
        "direction": "BUY",
        "entry_price": 100.0,
        "stop_loss": 95.0,
        "take_profit": 110.0,
        "confidence": 70,
        "skip_reason": "",
        "executed": 0,
        "outcome": "",
    }
    row.update(overrides)
    return row


class TestConflictClassification:
    def test_same_identity_and_material_is_clean(self):
        assert build_save_conflict_event(_instruction(), [_row()]) is None

    def test_same_identity_material_change_is_conflict(self):
        event = build_save_conflict_event(
            _instruction(direction="SKIP", skip_reason="guardrail"),
            [_row()],
        )
        assert event is not None
        assert "same_decision_id_material_conflict" in event["classification"]
        assert event["policy"].startswith("record-only")

    def test_da_ue_opposite_direction_is_channel_conflict(self):
        event = build_save_conflict_event(
            _instruction(decision_id="universe_expansion:2026-10-01:TEST"),
            [_row(direction="SELL_SHORT")],
        )
        assert event is not None
        assert "channel_direction_conflict" in event["classification"]
        assert event["channel_direction_conflicts"][0]["source"] == "DA"

    def test_skip_is_not_channel_direction_conflict(self):
        event = build_save_conflict_event(
            _instruction(direction="SKIP", skip_reason="confidence"),
            [_row(direction="SELL_SHORT")],
        )
        assert event is None or "channel_direction_conflict" not in event["classification"]

    def test_duplicate_identity_is_detected(self):
        event = build_save_conflict_event(
            _instruction(), [_row(id=1), _row(id=2)]
        )
        assert "duplicate_decision_id" in event["classification"]


class TestEventAppend:
    def test_append_is_idempotent(self, tmp_path):
        path = tmp_path / "shadow" / "conflict.jsonl"
        event = build_save_conflict_event(
            _instruction(direction="SKIP"), [_row()]
        )
        assert append_event(event, path) is True
        assert append_event(event, path) is False
        records = [json.loads(line) for line in path.read_text().splitlines()]
        assert len(records) == 1
        assert records[0]["event_id"] == event["event_id"]


class TestWriteBoundaryHook:
    @pytest.fixture
    def db(self):
        import sqlite3

        from app.playbook.db import PlaybookDB

        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        database = PlaybookDB(conn=connection)
        database.ensure_tables()
        yield database
        connection.close()

    def test_hook_records_without_changing_arbitration(self, db, monkeypatch):
        calls = []
        monkeypatch.setattr(
            "app.playbook.conflict_shadow.append_event",
            lambda event, path: calls.append(event) or True,
        )
        first = db.save_instruction(_instruction())
        second = db.save_instruction(
            _instruction(direction="SKIP", skip_reason="guardrail")
        )
        assert second == first
        assert len(calls) == 1
        assert "same_decision_id_material_conflict" in calls[0]["classification"]

    def test_hook_failure_never_blocks_save(self, db, monkeypatch):
        def boom(inst, existing):
            raise RuntimeError("simulated shadow failure")

        monkeypatch.setattr(
            "app.playbook.conflict_shadow.emit_save_conflict_shadow", boom
        )
        row_id = db.save_instruction(_instruction())
        assert row_id > 0


def test_save_instruction_contains_write_boundary_hook():
    from pathlib import Path

    source = Path(playbook_db.__file__).read_text(encoding="utf-8")
    assert "emit_save_conflict_shadow" in source
