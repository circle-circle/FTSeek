from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import scripts.llm_universe_scan as ue  # noqa: E402
from app.event_stream.models import CanonicalEvent  # noqa: E402
from app.event_stream.store import EventStore  # noqa: E402


def _event() -> CanonicalEvent:
    event_time = datetime.now(timezone.utc) - timedelta(hours=1)
    return CanonicalEvent(
        event_id="event-stream-1",
        source_id="TEST_SOURCE",
        source_tier="T0",
        source_url="https://example.gov/document",
        event_type="REGULATORY_APPROVAL",
        event_time=event_time.isoformat(),
        headline="SpaceX receives FCC authority for direct-to-cell satellites",
        summary="",
        facts={"source": "FCC"},
        status="UNVERIFIED",
        confidence=0.8,
        reasoning_version="v0",
    )


def test_event_stream_clues_are_relevant_structured_facts(tmp_path, monkeypatch):
    db_path = tmp_path / "event_stream.db"
    store = EventStore(db_path)
    store.append_canonical_event(_event())

    values = {
        "universe_expansion.event_stream_clue_enabled": True,
        "universe_expansion.event_stream_clue_lookback_hours": 24,
        "universe_expansion.event_stream_clue_scan_limit": 500,
        "universe_expansion.event_stream_clue_limit": 20,
        "universe_expansion.event_stream_clue_dedupe": True,
    }
    monkeypatch.setattr(ue, "_cfg", lambda path, default=None: values.get(path, default))
    facts, health = ue._gather_event_stream_clues(db_path)

    assert health == "EventStream:1"
    assert len(facts) == 1
    assert facts[0]["source"] == "event_stream"
    assert facts[0]["event_key"] == "event_stream/event-stream-1"
    assert set(facts[0]["symbols"]) == {"T", "TMUS", "VZ"}
    assert "direct-to-cell satellites" in facts[0]["text"]
    assert "source=FCC" in facts[0]["text"]


def test_event_stream_clue_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ue,
        "_cfg",
        lambda path, default=None: False
        if path == "universe_expansion.event_stream_clue_enabled"
        else default,
    )
    facts, health = ue._gather_event_stream_clues(tmp_path / "missing.db")
    assert facts == []
    assert health == "EventStream:off"


def test_build_clue_brief_registers_event_stream_evidence(monkeypatch, tmp_path):
    event = _event()
    fact = ue._fact(
        "event_stream",
        f"[T0/REGULATORY_APPROVAL] {event.event_time} | {event.headline} | "
        "source=FCC | event_id=event-stream-1",
        symbols=["T", "TMUS", "VZ"],
        published_at=event.event_time,
        event_key="event_stream/event-stream-1",
    )
    values = {
        "universe_expansion.event_stream_clue_enabled": True,
        "universe_expansion.clue_hard_events": False,
        "universe_expansion.clue_earnings_events": False,
        "universe_expansion.clue_insider_clusters": False,
        "universe_expansion.clue_8k_events": False,
        "universe_expansion.clue_13dg_events": False,
        "universe_expansion.clue_macro_brief": False,
        "universe_expansion.clue_event_symbols": False,
        "universe_expansion.clue_peer_surge": False,
        "universe_expansion.clue_premarket_rank": False,
        "universe_expansion.scan_regime_brief": False,
        "universe_expansion.input_prescreen": False,
    }
    monkeypatch.setattr(ue, "_cfg", lambda path, default=None: values.get(path, default))
    monkeypatch.setattr(ue, "_gather_news", lambda hours: [])
    monkeypatch.setattr(ue, "_gather_market_corroboration", lambda: {})
    monkeypatch.setattr(ue, "_build_feedback_table", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        ue, "_gather_event_stream_clues", lambda *args, **kwargs: ([fact], "EventStream:1")
    )
    monkeypatch.setattr(ue, "_ROOT", tmp_path)

    brief = ue._build_clue_brief("")
    assert "[EventStream 一手/发现线索 (observe-only, 相关性过滤+去重)]" in brief
    indexed = [entry for entry in ue.brief_evidence_index()
               if entry["source"] == "event_stream"]
    assert len(indexed) == 1
    assert indexed[0]["event_key"] == "event_stream/event-stream-1"
    assert set(indexed[0]["symbols"]) == {"T", "TMUS", "VZ"}
