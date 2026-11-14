from __future__ import annotations

import pytest

from app.event_stream.intelligence import EventRecord, build_event_snapshot
from app.event_stream.market_brief import (
    CHALLENGER_SYSTEM,
    MAIN_SYSTEM,
    PROMPT_VERSION,
    build_brief_context,
    build_main_prompt,
    generate_market_event_brief,
    normalize_challenger_payload,
    normalize_main_payload,
    validate_challenger,
    validate_event_snapshot,
    validate_transmissions,
)
from app.event_stream.models import CanonicalEvent


def make_snapshot(headline="AI stocks fall after election"):
    event = CanonicalEvent(
        event_id="event-1", source_id="NEWS", source_tier="T1",
        source_url="https://news.example/1", event_type="NEWS_WIRE",
        event_time="2026-11-05T10:00:00+00:00", headline=headline,
        summary="Data centres and chips split after the vote", facts={"source": "Wire"},
        status="UNVERIFIED", confidence=0.5, reasoning_version="v0",
    )
    record = EventRecord(event, "2026-11-05T11:00:00+00:00", 60000)
    return build_event_snapshot(
        [record], since="2026-11-05T00:00:00+00:00", as_of="2026-11-05T12:00:00+00:00",
    )


def valid_main():
    return normalize_main_payload({
        "transmissions": [{
            "event_ids": ["event-1"], "theme": "AI hardware", "sign": "NEGATIVE",
            "horizon": "INTRADAY", "price_in": "UNKNOWN", "invalidation": "new official denial",
            "causal_chain": "election to spending concern", "counter_evidence": "software may benefit",
            "confidence": 0.6, "beneficiary_symbols": ["GOOG"], "loser_symbols": ["NVDA"],
        }],
        "unknowns": ["need sector prices"],
    })


def test_snapshot_validator_rejects_future_event():
    snapshot = make_snapshot()
    snapshot["clusters"][0]["events"][0]["first_seen_at"] = "2026-11-05T13:00:00+00:00"
    assert "future_event:event-1" in validate_event_snapshot(snapshot)


def test_context_contains_traceable_fact_and_explicit_market_gap():
    context = build_brief_context(make_snapshot())
    assert context["facts"][0]["event_id"] == "event-1"
    assert context["facts"][0]["source_urls"] == ["https://news.example/1"]
    assert context["facts"][0]["member_facts"][0]["event_id"] == "event-1"
    assert context["market_evidence"] == []
    assert "market_evidence_not_connected:price_in_must_be_UNKNOWN" in context["unknowns"]


def test_target_universe_is_shared_with_main_prompt_without_changing_default():
    baseline = build_brief_context(make_snapshot())
    assert baseline["target_universe"] == []
    context = build_brief_context(
        make_snapshot(), target_universe=["US.AMD", "nvda", "US.AMD"],
    )
    assert context["target_universe"] == ["AMD", "NVDA"]
    _, user = build_main_prompt(context)
    assert '"target_universe": ["AMD", "NVDA"]' in user
    assert "不得仅凭同属行业" in build_main_prompt(context)[0]


def test_prompt_v4_requires_verbatim_ids_unknown_sign_and_counter_evidence():
    assert PROMPT_VERSION == "market_event_brief_v4"
    assert "逐字复制" in MAIN_SYSTEM
    assert "不得使用相似 ID" in CHALLENGER_SYSTEM
    assert "NEUTRAL/UNCLEAR/UNCERTAIN" in MAIN_SYSTEM
    assert "counter_evidence 不得为空字符串" in MAIN_SYSTEM
    assert "每条 counter_transmission" in CHALLENGER_SYSTEM
    payload = normalize_main_payload({"transmissions": [{"sign": "NEUTRAL"}, {"sign": "UNCERTAIN"}]})
    assert [item["sign"] for item in payload["transmissions"]] == ["UNKNOWN", "UNKNOWN"]


def test_macro_selection_prioritizes_election_ai_headline():
    snapshot = make_snapshot()
    context = build_brief_context(snapshot, limit=1)
    assert context["selection"]["selected_cluster_ids"] == ["event-1"]
    assert context["selection"]["omitted_cluster_count"] == 0


def make_two_cluster_snapshot():
    def record(event_id, headline):
        event = CanonicalEvent(
            event_id=event_id, source_id="NEWS", source_tier="T1",
            source_url=f"https://news.example/{event_id}", event_type="NEWS_WIRE",
            event_time="2026-11-05T10:00:00+00:00", headline=headline,
            summary="", facts={"source": "Wire"}, status="UNVERIFIED",
            confidence=0.5, reasoning_version="v0",
        )
        return EventRecord(event, "2026-11-05T11:00:00+00:00", 60000)

    return build_event_snapshot(
        [
            record("macro", "Election results spark debate"),
            record("key", "Cramer says CEO may be ousted"),
        ],
        since="2026-11-05T00:00:00+00:00", as_of="2026-11-05T12:00:00+00:00",
    )


def test_requested_priority_cluster_is_selected_and_recorded():
    snapshot = make_two_cluster_snapshot()
    baseline = build_brief_context(snapshot, limit=1)
    assert baseline["selection"]["selected_cluster_ids"] == ["macro"]

    context = build_brief_context(snapshot, limit=1, priority_cluster_ids={"key"})
    assert context["selection"]["selected_cluster_ids"] == ["key"]
    assert context["selection"]["requested_priority_cluster_ids"] == ["key"]
    assert context["selection"]["requested_priority_selected_cluster_ids"] == ["key"]
    assert context["selection"]["requested_priority_omitted_cluster_ids"] == []
    assert context["selection"]["requested_priority_unknown_cluster_ids"] == []


def test_unknown_requested_priority_cluster_is_recorded_not_fatal():
    context = build_brief_context(
        make_two_cluster_snapshot(), limit=1, priority_cluster_ids={"missing"},
    )
    assert context["selection"]["selected_cluster_ids"] == ["macro"]
    assert context["selection"]["requested_priority_selected_cluster_ids"] == []
    assert context["selection"]["requested_priority_omitted_cluster_ids"] == []
    assert context["selection"]["requested_priority_unknown_cluster_ids"] == ["missing"]


def test_delta_context_requires_lineage_and_carries_previous_brief():
    snapshot = make_snapshot()
    snapshot["delta"] = [{"change_type": "NEW", "event": {"event_id": "event-1"}}]
    previous = {"brief_id": "brief-old", "snapshot_id": "old-snapshot", "transmissions": [],
                "opposition": "NONE", "fatal_flaw": None}
    snapshot["supersedes"] = "old-snapshot"
    context = build_brief_context(snapshot, previous_brief=previous)
    assert context["supersedes_brief_id"] == "brief-old"
    assert context["selection"]["delta_selected_cluster_ids"] == ["event-1"]
    snapshot["supersedes"] = "different"
    with pytest.raises(ValueError, match="lineage"):
        build_brief_context(snapshot, previous_brief=previous)


def test_transmission_requires_grounded_event_and_market_evidence_for_price_in():
    payload = valid_main()
    payload["transmissions"][0]["event_ids"] = ["invented"]
    payload["transmissions"][0]["price_in"] = "PRICED"
    errors = validate_transmissions(payload["transmissions"], build_brief_context(make_snapshot()), prefix="t")
    assert "t[0]:ungrounded_event:invented" in errors
    assert "t[0]:price_in_without_market_evidence" in errors


def test_challenger_oppose_requires_evidence():
    context = build_brief_context(make_snapshot())
    payload = normalize_challenger_payload({"opposition": "OPPOSE", "detail": "wrong"})
    assert "oppose_requires_detail_and_evidence" in validate_challenger(payload, context)


def test_challenger_oppose_reuses_only_existing_fatal_flaw_evidence():
    context = build_brief_context(make_snapshot())
    payload = normalize_challenger_payload({
        "opposition": "OPPOSE", "detail": "unconfirmed event and opposite policy evidence",
        "fatal_flaw": {"detail": "same objection", "evidence_ids": ["event-1"]},
    })
    assert payload["evidence_ids"] == ["event-1"]
    assert validate_challenger(payload, context) == []
    # A fatal flaw without evidence must not manufacture a top-level citation.
    missing = normalize_challenger_payload({
        "opposition": "OPPOSE", "detail": "unsupported",
        "fatal_flaw": {"detail": "unsupported", "evidence_ids": []},
    })
    assert "oppose_requires_detail_and_evidence" in validate_challenger(missing, context)


def test_fatal_flaw_cannot_use_invented_evidence():
    context = build_brief_context(make_snapshot())
    payload = normalize_challenger_payload({
        "opposition": "OPPOSE", "detail": "wrong", "evidence_ids": ["event-1"],
        "fatal_flaw": {"detail": "bad chain", "evidence_ids": ["future-event"]},
    })
    assert "ungrounded_fatal_flaw_event:future-event" in validate_challenger(payload, context)


def test_dry_run_makes_zero_calls_and_does_not_fake_success():
    brief, artifacts = generate_market_event_brief(build_brief_context(make_snapshot()))
    assert brief["llm_calls"] == 0
    assert brief["transmissions"] == []
    assert brief["degraded"] is True
    assert brief["validation_errors"] == ["llm_not_executed"]
    assert artifacts["main_raw"] is None


def test_dual_llm_generates_grounded_brief_and_call_metadata():
    responses = [valid_main(), {
        "opposition": "OPPOSE", "detail": "sector split not universal", "evidence_ids": ["event-1"],
        "fatal_flaw": None, "counter_transmissions": [], "missing_queries": ["sector prices"],
        "_model_reported": "deepseek-v4.1-flash", "_usage": {"total_tokens": 20},
    }]
    calls = []

    def fake_call(system, user, **kwargs):
        calls.append(kwargs)
        response = dict(responses.pop(0))
        response.setdefault("_model_reported", kwargs["model"])
        response.setdefault("_usage", {"total_tokens": 10})
        return response

    brief, artifacts = generate_market_event_brief(
        build_brief_context(make_snapshot()), llm_call=fake_call,
    )
    assert brief["llm_calls"] == 2
    assert brief["validation_errors"] == []
    assert brief["opposition"] == "OPPOSE"
    assert brief["arbitration_required"] is False
    assert brief["transmissions"][0]["loser_symbols"] == ["NVDA"]
    assert "missing_query:sector prices" in brief["unknowns"]
    assert calls[0]["model"] == "qwen3.7-max"
    assert calls[1]["model"] == "deepseek-v4.1-flash"
    assert artifacts["main_raw"]["_model_reported"] == "qwen3.7-max"


def test_unresolved_fatal_flaw_is_explicit_not_silently_appended():
    responses = [valid_main(), {
        "opposition": "OPPOSE", "detail": "wrong causal chain", "evidence_ids": ["event-1"],
        "fatal_flaw": {"detail": "unsupported universal short", "evidence_ids": ["event-1"]},
    }]

    def fake_call(system, user, **kwargs):
        return responses.pop(0)

    brief, _ = generate_market_event_brief(
        build_brief_context(make_snapshot()), llm_call=fake_call,
    )
    assert brief["arbitration_required"] is True
    assert brief["fatal_flaw"]["evidence_ids"] == ["event-1"]


def test_main_failure_skips_challenger_and_stays_degraded():
    calls = []

    def failing_call(system, user, **kwargs):
        calls.append(kwargs)
        return {"_error": "timeout", "_model_reported": kwargs["model"]}

    brief, _ = generate_market_event_brief(
        build_brief_context(make_snapshot()), llm_call=failing_call,
    )
    assert brief["llm_calls"] == 1
    assert len(calls) == 1
    assert brief["degraded"] is True
    assert brief["validation_errors"] == [
        "main_call_failed:timeout", "main_missing_transmissions", "main_missing_unknowns",
    ]


def test_malformed_llm_payload_is_not_accepted_as_empty_success():
    brief, _ = generate_market_event_brief(
        build_brief_context(make_snapshot()), llm_call=lambda *args, **kwargs: {},
    )
    assert "main_missing_transmissions" in brief["validation_errors"]
    assert "main_missing_unknowns" in brief["validation_errors"]
    assert brief["degraded"] is True


def test_string_fatal_flaw_is_preserved_as_unresolved_error():
    responses = [valid_main(), {"opposition": "OPPOSE", "detail": "bad", "evidence_ids": ["event-1"],
                                "fatal_flaw": "unsupported"}]

    def fake_call(system, user, **kwargs):
        return responses.pop(0)

    brief, _ = generate_market_event_brief(
        build_brief_context(make_snapshot()), llm_call=fake_call,
    )
    assert brief["arbitration_required"] is True
    assert "fatal_flaw_requires_evidence" in brief["validation_errors"]
