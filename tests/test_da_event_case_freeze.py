from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest

from app.event_stream.intelligence import EventRecord, build_event_snapshot
from app.event_stream.market_brief import build_brief_context
from app.event_stream.models import CanonicalEvent
from app.event_stream.store import EventStore
from scripts.da_event_case_freeze import (
    _parse_publish,
    checkpoint_entry,
    collect_post_cutoff_probes,
    evaluate_case,
    main,
    symbol_mentioned,
)

UTC = timezone.utc
KEY_EVENT_ID = "853ef3ee05478043a7c9"


def make_record(event_id: str, first_seen_at: str, headline: str, *, source_id: str = "TEST_NEWS"):
    event = CanonicalEvent(
        event_id=event_id,
        source_id=source_id,
        source_tier="T1",
        source_url=f"https://news.example/{event_id}",
        event_type="NEWS_WIRE",
        event_time=first_seen_at,
        headline=headline,
        summary="",
        facts={"source": "Publisher A"},
        status="UNVERIFIED",
        confidence=0.5,
        reasoning_version="v0",
    )
    return EventRecord(event, first_seen_at, 60000)


def make_comparison_snapshot():
    records = [
        make_record(KEY_EVENT_ID, "2026-11-12T01:44:03+00:00",
                    "Cramer's stance on Nvidia's stock amid reports that CEO Jensen Huang may be ousted"),
        make_record("macro-1", "2026-11-12T02:00:00+00:00",
                    "Election results spark debate"),
    ]
    return build_event_snapshot(
        records, since="2026-11-11T13:15:00+00:00", as_of="2026-11-12T13:15:00+00:00",
    )


def test_symbol_mentioned_uses_word_boundaries():
    assert symbol_mentioned("Nvidia board prepares to oust Huang", "NVDA")
    assert symbol_mentioned("NVDA falls 8%", "NVDA")
    assert symbol_mentioned("Arm Holdings jumps", "ARM")
    assert not symbol_mentioned("alarm bells ring", "ARM")
    assert not symbol_mentioned("HYDROFARM files an 8-K", "ARM")
    assert symbol_mentioned("Micron rises", "MU")
    assert symbol_mentioned("TSMC gains", "TSM")
    assert symbol_mentioned("TSM gains", "TSM")


def test_evaluate_case_requires_visible_key_and_excludes_post_cutoff_probe():
    snapshot = make_comparison_snapshot()
    brief = build_brief_context(
        snapshot, limit=48, priority_cluster_ids=[KEY_EVENT_ID],
    )
    prior = build_event_snapshot(
        [
            make_record("macro-1", "2026-11-11T02:00:00+00:00", "Election results spark debate"),
        ],
        since="2026-11-10T13:15:00+00:00", as_of="2026-11-11T13:15:00+00:00",
    )
    result = evaluate_case(
        snapshot=snapshot,
        cutoff="2026-11-12T13:15:00+00:00",
        key_event_ids=[KEY_EVENT_ID],
        post_cutoff_probe_ids=["future-1"],
        brief_context=brief,
        prior_snapshot=prior,
    )
    assert result["passed"] is True
    assert result["checks"]["key_events_selected_in_brief"] is True
    assert result["checks"]["key_events_absent_at_prior_cutoff"] is True

    leak = evaluate_case(
        snapshot=snapshot,
        cutoff="2026-11-12T13:15:00+00:00",
        key_event_ids=[KEY_EVENT_ID],
        post_cutoff_probe_ids=[KEY_EVENT_ID],
        brief_context=brief,
        prior_snapshot=prior,
    )
    assert leak["checks"]["post_cutoff_probes_excluded"] is False
    assert leak["passed"] is False

    missing = evaluate_case(
        snapshot=snapshot,
        cutoff="2026-11-12T13:15:00+00:00",
        key_event_ids=["not-visible"],
        post_cutoff_probe_ids=["future-1"],
        brief_context=brief,
        prior_snapshot=prior,
    )
    assert missing["checks"]["key_events_visible"] is False
    assert missing["checks"]["key_events_selected_in_brief"] is False


def test_evaluate_case_rejects_a_future_first_seen():
    snapshot = make_comparison_snapshot()
    brief = build_brief_context(snapshot, limit=48, priority_cluster_ids=[KEY_EVENT_ID])
    snapshot["clusters"][0]["events"][0]["first_seen_at"] = "2026-11-12T13:15:01+00:00"
    result = evaluate_case(
        snapshot=snapshot,
        cutoff="2026-11-12T13:15:00+00:00",
        key_event_ids=[KEY_EVENT_ID],
        post_cutoff_probe_ids=["future-1"],
        brief_context=brief,
    )
    assert result["checks"]["no_future_events"] is False
    assert result["checks"]["snapshot_grounded"] is False
    with pytest.raises(ValueError, match="future_event"):
        build_brief_context(snapshot, limit=48, priority_cluster_ids=[KEY_EVENT_ID])


def test_parse_publish_accepts_unix_and_iso():
    assert _parse_publish("1794488819") == datetime.fromtimestamp(1794488819, UTC)
    assert _parse_publish("2026-11-12T13:20:00Z") == datetime(2026, 11, 12, 13, 20, tzinfo=UTC)
    assert _parse_publish(None) is None
    assert _parse_publish("not-a-time") is None


def test_checkpoint_entry_hashes_and_flags_post_cutoff_news(tmp_path):
    raw = {
        "symbol": "US.NVDA",
        "trade_date": "2026-11-12",
        "saved_at": "2026-11-12T13:17:02+00:00",
        "collected": {
            "_timestamp": 1794489268.2292926,
            "news": [
                {"title": "Nvidia before cutoff", "publish_time": "1794488819"},
                {"title": "AMD after cutoff", "publish_time": "1794489540"},
            ],
            "news_v2": [
                {"title": "Huang report", "publish_time": "2026-11-12T07:51:46Z"},
            ],
        },
    }
    path = tmp_path / "US.NVDA.raw.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    cutoff = datetime(2026, 11, 12, 13, 15, tzinfo=UTC)
    entry = checkpoint_entry(path, symbol="US.NVDA", cutoff=cutoff)
    assert entry["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert entry["trade_date"] == "2026-11-12"
    assert entry["collected_at"].startswith("2026-11-12T13:14:28")
    assert entry["collected_news_item_count"] == 3
    assert [row["title"] for row in entry["news_after_cutoff"]] == ["AMD after cutoff"]
    assert entry["nvidia_or_huang_news_count"] == 2


def test_collect_post_cutoff_probes_reads_only_later_events(tmp_path):
    db_path = tmp_path / "events.db"
    store = EventStore(db_path)
    store.append_canonical_event(
        make_record("pre", "2026-11-12T13:00:00+00:00", "Nvidia pre-cutoff").event,
        first_seen_at="2026-11-12T13:00:00+00:00",
    )
    store.append_canonical_event(
        make_record("future-nvda", "2026-11-12T13:24:10+00:00", "Nvidia post-cutoff").event,
        first_seen_at="2026-11-12T13:24:10+00:00",
    )
    store.append_canonical_event(
        make_record("future-other", "2026-11-12T13:30:00+00:00", "Oil slides").event,
        first_seen_at="2026-11-12T13:30:00+00:00",
    )
    probes = collect_post_cutoff_probes(
        db_path, cutoff="2026-11-12T13:15:00+00:00", horizon_hours=3,
    )
    assert [row["event_id"] for row in probes] == ["future-nvda"]
    assert probes[0]["first_seen_at"] > "2026-11-12T13:15:00+00:00"


def test_main_refuses_existing_output(tmp_path):
    output = tmp_path / "case"
    output.mkdir()
    with pytest.raises(SystemExit, match="拒绝覆盖"):
        main([
            "--as-of", "2026-11-12T13:15:00+00:00",
            "--raw-root", str(tmp_path),
            "--symbols", "NVDA",
            "--key-event-ids", KEY_EVENT_ID,
            "--output-dir", str(output),
        ])


def test_main_freezes_v2_with_supplemental_evidence(tmp_path):
    db_path = tmp_path / "events.db"
    store = EventStore(db_path)
    store.append_canonical_event(
        make_record(
            KEY_EVENT_ID, "2026-11-12T01:44:03+00:00",
            "Cramer's stance on Nvidia's stock amid reports that CEO Jensen Huang may be ousted",
        ).event,
        first_seen_at="2026-11-12T01:44:03+00:00",
    )
    store.append_canonical_event(
        make_record("future-1", "2026-11-12T13:30:00+00:00", "Nvidia post-cutoff").event,
        first_seen_at="2026-11-12T13:30:00+00:00",
    )
    raw_root = tmp_path / "raw"
    raw_root.mkdir()
    nvda_raw = {
        "symbol": "US.NVDA", "trade_date": "2026-11-12",
        "saved_at": "2026-11-12T13:17:02+00:00",
        "collected": {
            "_timestamp": 1794489268.0,
            "news": [{
                "title": "Nvidia board may oust Huang",
                "publish_time": "2026-11-12T11:00:00Z",
                "url": "https://news.example/nvda",
            }],
            "news_v2": [],
        },
        "user_prompt": "raw prompt NVDA",
    }
    amd_raw = {
        "symbol": "US.AMD", "trade_date": "2026-11-12",
        "saved_at": "2026-11-12T13:24:35+00:00",
        "collected": {
            "_timestamp": 1794489731.0,
            "news": [{
                "title": "AMD, Broadcom gain after reports say Nvidia board preparing to oust Huang",
                "publish_time": "2026-11-12T11:00:00Z",
                "url": "https://news.example/amd",
            }, {
                "title": "AMD date only note",
                "publish_time": "2026-11-12",
                "url": "https://news.example/amd-date-only",
            }],
            "news_v2": [],
        },
        "user_prompt": "raw prompt AMD",
    }
    (raw_root / "US.NVDA.raw.json").write_text(json.dumps(nvda_raw), encoding="utf-8")
    (raw_root / "US.AMD.raw.json").write_text(json.dumps(amd_raw), encoding="utf-8")
    output = tmp_path / "case_v2"
    rc = main([
        "--db", str(db_path),
        "--as-of", "2026-11-12T13:26:00+00:00",
        "--prior-as-of", "2026-11-11T13:15:00+00:00",
        "--lookback-hours", "24",
        "--raw-root", str(raw_root),
        "--symbols", "NVDA,AMD",
        "--target-symbols", "NVDA,AMD",
        "--key-event-ids", KEY_EVENT_ID,
        "--supplemental-raw-root", str(raw_root),
        "--supplemental-cutoff", "2026-11-12T13:26:00+00:00",
        "--supplemental-limit", "0",
        "--retained-case-dir", str(tmp_path / "no-retained-case"),
        "--output-dir", str(output),
    ])
    assert rc == 0
    manifest = json.loads((output / "case_manifest.json").read_text(encoding="utf-8"))
    assert manifest["passed"] is True
    assert manifest["as_of"] == "2026-11-12T13:26:00+00:00"
    supplemental = manifest["supplemental_evidence"]
    assert supplemental["cutoff"] == "2026-11-12T13:26:00+00:00"
    assert supplemental["stats"]["unique_evidence_count"] >= 2
    assert supplemental["stats"]["date_only_item_count"] == 1
    assert supplemental["stats"]["rejected_future_count"] == 0
    assert manifest["checks"]["supplemental_grounded"] is True
    assert manifest["checks"]["supplemental_no_future"] is True
    assert manifest["checks"]["raw_user_prompts_recorded"] is True
    assert (output / "supplemental_evidence.json").exists()
    context = json.loads((output / "brief_context.json").read_text(encoding="utf-8"))
    supplemental_facts = [fact for fact in context["facts"] if fact.get("supplemental")]
    assert supplemental_facts
    assert all(fact["event_id"].startswith("SUP-") for fact in supplemental_facts)
    date_only = [
        fact for fact in supplemental_facts
        if fact["event_time_precision"] == "DATE_ONLY"
    ]
    assert date_only and date_only[0]["event_time"] == "2026-11-12"
    assert "T" not in date_only[0]["event_time"]
    assert context["target_universe"] == ["AMD", "NVDA"]


def test_main_freezes_a_minimal_case_end_to_end(tmp_path):
    db_path = tmp_path / "events.db"
    store = EventStore(db_path)
    store.append_canonical_event(
        make_record(
            KEY_EVENT_ID, "2026-11-12T01:44:03+00:00",
            "Cramer's stance on Nvidia's stock amid reports that CEO Jensen Huang may be ousted",
        ).event,
        first_seen_at="2026-11-12T01:44:03+00:00",
    )
    store.append_canonical_event(
        make_record("future-1", "2026-11-12T13:24:10+00:00", "Nvidia post-cutoff").event,
        first_seen_at="2026-11-12T13:24:10+00:00",
    )
    raw_root = tmp_path / "raw"
    raw_root.mkdir()
    (raw_root / "US.NVDA.raw.json").write_text(json.dumps({
        "symbol": "US.NVDA",
        "trade_date": "2026-11-12",
        "saved_at": "2026-11-12T13:17:02+00:00",
        "collected": {"_timestamp": 1794489268.0, "news": [], "news_v2": []},
    }), encoding="utf-8")
    output = tmp_path / "case"
    rc = main([
        "--db", str(db_path),
        "--as-of", "2026-11-12T13:15:00+00:00",
        "--prior-as-of", "2026-11-11T13:15:00+00:00",
        "--lookback-hours", "24",
        "--raw-root", str(raw_root),
        "--symbols", "NVDA",
        "--key-event-ids", KEY_EVENT_ID,
        "--output-dir", str(output),
    ])
    assert rc == 0
    manifest = json.loads((output / "case_manifest.json").read_text(encoding="utf-8"))
    assert manifest["passed"] is True
    assert manifest["event_visibility"]["key_events"][0]["selected_in_brief"] is True
    assert manifest["event_visibility"]["post_cutoff_probe_events"][0]["excluded_from_snapshot"] is True
    assert manifest["prior_checkpoint"]["key_events_visible"] == []
    assert (output / "SHA256SUMS.txt").exists()