from __future__ import annotations

import json

import pytest

from app.event_stream.intelligence import (
    CursorState,
    EventRecord,
    advance_cursor,
    build_event_snapshot,
    parse_timestamp,
    read_event_records,
    run_event_replay,
)
from app.event_stream.models import CanonicalEvent
from app.event_stream.store import EventStore
from scripts.da_event_intelligence import _write_json_new


def make_record(
    event_id: str,
    first_seen_at: str,
    *,
    headline: str | None = None,
    source_id: str = "TEST_NEWS",
    source_tier: str = "T1",
    event_time: str | None = "2026-11-05T00:00:00+00:00",
    status: str = "UNVERIFIED",
    latency_ms: int | None = 60000,
    publisher: str = "Publisher A",
    summary: str = "summary",
) -> EventRecord:
    event = CanonicalEvent(
        event_id=event_id,
        source_id=source_id,
        source_tier=source_tier,
        source_url=f"https://{source_id.lower()}.example/{event_id}",
        event_type="NEWS_WIRE",
        event_time=event_time,
        headline=headline or f"Headline {event_id}",
        summary=summary,
        facts={"source": publisher},
        status=status,
        confidence=0.5,
        reasoning_version="v0",
    )
    return EventRecord(event, first_seen_at, latency_ms)


def test_as_of_is_inclusive_and_excludes_future_and_old_events():
    records = [
        make_record("old", "2026-11-04T00:00:00+00:00"),
        make_record("boundary", "2026-11-05T13:35:00+00:00"),
        make_record("future", "2026-11-05T13:35:01+00:00"),
    ]
    snapshot = build_event_snapshot(
        records,
        since="2026-11-04T12:00:00+00:00",
        as_of="2026-11-05T08:35:00-05:00",
    )
    assert snapshot["event_count"] == 1
    assert snapshot["clusters"][0]["event_ids"] == ["boundary"]
    assert snapshot["dedupe_mode"] == "exact_url_title_v1"
    assert snapshot["llm_calls"] == 0


def test_naive_as_of_is_rejected():
    with pytest.raises(ValueError, match="timezone required"):
        parse_timestamp("2026-11-05T08:35:00", require_timezone=True)


def test_snapshot_is_deterministic_and_has_latency_units():
    records = [
        make_record("a", "2026-11-05T12:00:00+00:00", latency_ms=0),
        make_record("b", "2026-11-05T12:01:00+00:00", latency_ms=3600000),
        make_record("c", "2026-11-05T12:02:00+00:00", latency_ms=7200000),
        make_record("d", "2026-11-05T12:03:00+00:00", latency_ms=None),
        make_record("e", "2026-11-05T12:04:00+00:00", latency_ms=-1),
    ]
    args = {"since": "2026-11-05T00:00:00+00:00", "as_of": "2026-11-05T13:00:00+00:00"}
    first = build_event_snapshot(records, **args)
    second = build_event_snapshot(list(reversed(records)), **args)
    assert first == second
    latency = first["latency"]["ALL"]
    assert latency == {
        "unit": "milliseconds",
        "event_count": 5,
        "valid_latency_count": 3,
        "missing_latency_count": 1,
        "negative_latency_count": 1,
        "p50_ms": 3600000.0,
        "p90_ms": 6480000.0,
        "max_ms": 7200000.0,
        "over_60m_count": 1,
        "over_60m_ratio_of_valid": pytest.approx(1 / 3, 6),
    }


def test_corroboration_requires_independent_publishers():
    records = [
        make_record("a", "2026-11-05T12:00:00+00:00", headline="Shared market headline", source_id="NEWS_A"),
        make_record("b", "2026-11-05T12:01:00+00:00", headline="Shared market headline", source_id="NEWS_B"),
    ]
    snapshot = build_event_snapshot(
        records, since="2026-11-05T00:00:00+00:00", as_of="2026-11-05T13:00:00+00:00",
    )
    assert snapshot["cluster_count"] == 1
    assert snapshot["clusters"][0]["corroboration"] == 1
    assert snapshot["clusters"][0]["status"] == "UNVERIFIED"
    changed = make_record(
        "c", "2026-11-05T12:02:00+00:00", headline="Shared market headline",
        source_id="NEWS_C", publisher="Publisher B",
    )
    corroborated = build_event_snapshot(
        records + [changed], since="2026-11-05T00:00:00+00:00", as_of="2026-11-05T13:00:00+00:00",
    )
    assert corroborated["clusters"][0]["corroboration"] == 2
    assert corroborated["clusters"][0]["status"] == "CORROBORATED"


def test_source_health_is_as_of_and_marks_future_rows_unknown():
    health = [{"source_id": "TEST_NEWS", "status": "OK", "latency_ms": 10,
               "last_success_at": "2026-11-06T00:00:00+00:00", "last_error": None}]
    snapshot = build_event_snapshot(
        [make_record("a", "2026-11-05T12:00:00+00:00")],
        since="2026-11-05T00:00:00+00:00", as_of="2026-11-05T13:00:00+00:00",
        source_health=health, expected_sources=["TEST_NEWS"],
    )
    assert snapshot["source_health"][0]["status"] == "UNKNOWN_AS_OF"
    assert snapshot["source_health"][0]["last_error"] is None
    assert snapshot["degraded"] is True


def test_source_health_error_and_missing_source_are_explicit():
    snapshot = build_event_snapshot(
        [], since="2026-11-05T00:00:00+00:00", as_of="2026-11-05T13:00:00+00:00",
        source_health=[{"source_id": "BROKEN", "status": "ERROR", "latency_ms": 1,
                        "last_success_at": "2026-11-05T12:59:00+00:00", "last_error": "boom"}],
        expected_sources=["BROKEN", "MISSING"],
    )
    assert {row["status"] for row in snapshot["source_health"]} == {"ERROR", "UNKNOWN_AS_OF"}
    assert snapshot["event_count"] == 0
    assert len(snapshot["unknowns"]) == 2


def test_cursor_deduplicates_overlap_without_advancing_empty_highwater():
    state = CursorState()
    state, empty = advance_cursor(state, [], as_of="2026-11-05T12:00:00+00:00")
    assert empty == []
    assert state.highwater_first_seen_at is None
    record = make_record("a", "2026-11-05T12:00:00+00:00")
    state, first = advance_cursor(state, [record], as_of="2026-11-05T12:00:00+00:00")
    assert [item["change_type"] for item in first] == ["NEW"]
    state, duplicate = advance_cursor(state, [record], as_of="2026-11-05T12:01:00+00:00")
    assert duplicate == []
    assert state.last_as_of == "2026-11-05T12:01:00+00:00"
    assert state.highwater_event_id == "a"


def test_cursor_detects_revision_and_retraction():
    original = make_record("a", "2026-11-05T12:00:00+00:00")
    state, _ = advance_cursor(CursorState(), [original], as_of="2026-11-05T12:00:00+00:00")
    revised = make_record("a", "2026-11-05T12:01:00+00:00", summary="revised")
    state, update = advance_cursor(state, [original, revised], as_of="2026-11-05T12:01:00+00:00")
    assert [item["change_type"] for item in update] == ["UPDATED"]
    retracted = make_record(
        "a", "2026-11-05T12:02:00+00:00", summary="revised", status="RETRACTED",
    )
    _, removal = advance_cursor(state, [retracted], as_of="2026-11-05T12:02:00+00:00")
    assert [item["change_type"] for item in removal] == ["RETRACTED"]


def test_replay_emits_no_new_snapshot_or_llm_call_when_unchanged():
    record = make_record("a", "2026-11-05T12:00:00+00:00")
    replay = run_event_replay(
        [record],
        checkpoints=["2026-11-05T12:00:00+00:00", "2026-11-05T12:05:00+00:00"],
        since="2026-11-05T00:00:00+00:00",
    )
    assert len(replay["snapshots"]) == 1
    assert [step["status"] for step in replay["steps"]] == ["SNAPSHOT", "NO_CHANGE"]
    assert replay["llm_calls"] == 0
    assert sum(step["llm_calls"] for step in replay["steps"]) == 0


def test_replay_late_arrival_appears_only_at_later_checkpoint():
    early = make_record("early", "2026-11-05T12:00:00+00:00")
    late = make_record("late", "2026-11-05T13:00:00+00:00")
    replay = run_event_replay(
        [early, late],
        checkpoints=["2026-11-05T12:30:00+00:00", "2026-11-05T13:30:00+00:00"],
        since="2026-11-05T00:00:00+00:00",
    )
    assert replay["snapshots"][0]["event_count"] == 1
    assert replay["snapshots"][1]["event_count"] == 2
    assert replay["snapshots"][1]["supersedes"] == replay["snapshots"][0]["snapshot_id"]
    assert replay["steps"][1]["delta_counts"] == {"NEW": 1}


def test_conflicting_revision_at_same_first_seen_fails_closed():
    first = make_record("a", "2026-11-05T12:00:00+00:00", summary="one")
    second = make_record("a", "2026-11-05T12:00:00+00:00", summary="two")
    with pytest.raises(ValueError, match="conflicting revisions"):
        build_event_snapshot(
            [first, second], since="2026-11-05T00:00:00+00:00", as_of="2026-11-05T13:00:00+00:00",
        )


def test_read_event_records_uses_real_schema_and_as_of(tmp_path):
    store = EventStore(tmp_path / "events.db")
    for event_id, seen in (("a", "2026-11-05T12:00:00+00:00"), ("b", "2026-11-05T13:00:00+00:00")):
        record = make_record(event_id, seen)
        store.append_canonical_event(record.event, first_seen_at=seen)
    records = read_event_records(
        tmp_path / "events.db",
        since="2026-11-05T00:00:00+00:00",
        until="2026-11-05T12:30:00+00:00",
    )
    assert [item.event.event_id for item in records] == ["a"]
    assert records[0].published_to_seen_ms is not None


def test_output_writer_refuses_clobber(tmp_path):
    path = tmp_path / "snapshot.json"
    _write_json_new(path, {"value": 1})
    with pytest.raises(FileExistsError):
        _write_json_new(path, {"value": 2})
    assert json.loads(path.read_text()) == {"value": 1}
