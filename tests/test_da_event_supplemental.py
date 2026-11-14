from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.event_stream.intelligence import EventRecord, build_event_snapshot
from app.event_stream.market_brief import build_brief_context
from app.event_stream.models import CanonicalEvent
from scripts.da_event_supplemental import (
    CASE_SYMBOLS,
    extract_supplemental_evidence,
    merge_supplemental_into_context,
    stable_evidence_id,
    validate_supplemental_evidence,
)

UTC = timezone.utc
CUTOFF = "2026-11-12T13:26:00+00:00"


def ts(hour, minute, second=0):
    return datetime(2026, 11, 12, hour, minute, second, tzinfo=UTC).timestamp()


def _raw(
    symbol: str,
    *,
    collection_ts: float,
    news=None,
    news_v2=None,
    saved_at: str = "2026-11-12T13:30:00+00:00",
    user_prompt: str = "raw prompt",
):
    return {
        "symbol": f"US.{symbol}",
        "trade_date": "2026-11-12",
        "saved_at": saved_at,
        "collected": {
            "_timestamp": collection_ts,
            "news": news or [],
            "news_v2": news_v2 or [],
        },
        "user_prompt": user_prompt,
    }


def _write_root(tmp_path, payloads):
    root = tmp_path / "raw"
    root.mkdir()
    for symbol, payload in payloads.items():
        (root / f"US.{symbol}.raw.json").write_text(json.dumps(payload), encoding="utf-8")
    return root


def test_stable_evidence_id_ignores_tracking_and_is_repeatable():
    first = stable_evidence_id(
        "AMD, Broadcom gain after reports say Nvidia board preparing to oust Huang",
        "https://news.example/a?utm_source=x&futusource=y", "2026-11-12",
    )
    second = stable_evidence_id(
        "AMD, Broadcom gain after reports say Nvidia board preparing to oust Huang",
        "https://news.example/a", "2026-11-12",
    )
    assert first == second
    assert first.startswith("SUP-")
    assert stable_evidence_id("", "", None) is None


def test_extract_dedups_across_checkpoints_and_keeps_member_facts(tmp_path):
    title = "AMD, Broadcom gain after reports say Nvidia board preparing to oust Huang"
    (tmp_path / "raw").mkdir()
    root = tmp_path / "raw"
    payloads = {
        "NVDA": _raw("NVDA", collection_ts=ts(13, 14, 28), news=[{
            "title": title, "publish_time": "2026-11-12T11:00:00Z",
            "url": "https://news.example/a?utm_source=x",
        }]),
        "AMD": _raw("AMD", collection_ts=ts(13, 22, 11), news=[{
            "title": title, "publish_time": "2026-11-12T11:00:00Z",
            "url": "https://news.example/a",
        }]),
        "AVGO": _raw("AVGO", collection_ts=ts(13, 20, 46), news_v2=[{
            "title": title, "publish_time": "2026-11-12T11:00:00Z",
            "source": "Yahoo", "url": "https://news.example/a",
        }]),
    }
    for symbol, payload in payloads.items():
        root.joinpath(f"US.{symbol}.raw.json").write_text(json.dumps(payload), encoding="utf-8")

    first = extract_supplemental_evidence(
        root, cutoff=CUTOFF, case_symbols=("NVDA", "AMD", "AVGO"),
    )
    second = extract_supplemental_evidence(
        root, cutoff=CUTOFF, case_symbols=("AVGO", "AMD", "NVDA"),
    )
    assert first["stats"]["unique_evidence_count"] == 1
    item = first["items"][0]
    assert item["evidence_id"] == second["items"][0]["evidence_id"]
    assert first["evidence_hash"] == second["evidence_hash"]
    assert item["source_symbols"] == ["US.AMD", "US.AVGO", "US.NVDA"]
    assert len(item["member_facts"]) == 3
    assert len(item["raw_paths"]) == 3
    assert len(item["raw_sha256s"]) == 3
    assert first["stats"]["duplicates_collapsed"] >= 2
    assert "NVDA" in item["mentioned_symbols"]
    assert "AMD" in item["mentioned_symbols"]
    assert "AVGO" in item["mentioned_symbols"]


def test_date_only_uses_collection_time_as_first_seen_not_midnight(tmp_path):
    root = _write_root(tmp_path, {
        "NVDA": _raw("NVDA", collection_ts=ts(13, 14, 28), news_v2=[{
            "title": "Nvidia date only report",
            "publish_time": "2026-11-12",
            "source": "Yahoo",
            "url": "https://news.example/date-only",
        }]),
        "AMD": _raw("AMD", collection_ts=ts(13, 22, 11), news=[{
            "title": "AMD date only report",
            "publish_time": "",
            "time": "11-12",
            "url": "https://news.example/amd-date-only",
        }]),
    })
    result = extract_supplemental_evidence(
        root, cutoff=CUTOFF, case_symbols=("NVDA", "AMD"),
    )
    assert result["stats"]["date_only_item_count"] == 2
    for item in result["items"]:
        assert item["event_time_precision"] == "DATE_ONLY"
        assert item["event_time"] == "2026-11-12"
        assert "T" not in item["event_time"]
        assert item["first_seen_at"].startswith("2026-11-12T13:")


def test_extract_rejects_future_publish_and_future_collection(tmp_path):
    root = _write_root(tmp_path, {
        "NVDA": _raw("NVDA", collection_ts=ts(13, 14, 28), news=[{
            "title": "Nvidia future publish",
            "publish_time": "2026-11-12T13:30:00Z",
            "url": "https://news.example/future",
        }]),
        # collection at 13:27 UTC, after the 13:26 cutoff
        "AMD": _raw("AMD", collection_ts=ts(13, 27, 0), news=[{
            "title": "AMD collected too late",
            "publish_time": "2026-11-12T13:00:00Z",
            "url": "https://news.example/late",
        }]),
    })
    result = extract_supplemental_evidence(
        root, cutoff=CUTOFF, case_symbols=("NVDA", "AMD"),
    )
    assert result["items"] == []
    assert result["stats"]["rejected_future_count"] == 2
    assert {row["reason"] for row in result["rejected"]} == {"future_publish_or_collection"}
    context = _base_context()
    with pytest.raises(ValueError, match="future"):
        merge_supplemental_into_context(context, result, cutoff=CUTOFF)


def test_validate_rejects_bad_grounding_and_future_member(tmp_path):
    root = _write_root(tmp_path, {
        "NVDA": _raw("NVDA", collection_ts=ts(13, 14, 28), news=[{
            "title": "Nvidia grounded item",
            "publish_time": "2026-11-12T11:00:00Z",
            "url": "https://news.example/ok",
        }]),
    })
    result = extract_supplemental_evidence(root, cutoff=CUTOFF, case_symbols=("NVDA",))
    assert validate_supplemental_evidence(result, cutoff=CUTOFF, case_symbols=("NVDA",)) == []

    result["items"][0]["source_symbols"] = ["US.AMZN"]
    result["items"][0]["member_facts"][0]["raw_sha256"] = ""
    result["items"][0]["member_facts"][0]["first_seen_at"] = "2026-11-12T13:30:00+00:00"
    errors = validate_supplemental_evidence(result, cutoff=CUTOFF, case_symbols=("NVDA",))
    assert any(error.startswith("unknown_source_symbol:") for error in errors)
    assert any(error.startswith("missing_member_provenance:") for error in errors)
    assert any(error.startswith("future_member_first_seen:") for error in errors)


def _make_record(event_id="direct-1"):
    event = CanonicalEvent(
        event_id=event_id, source_id="NEWS", source_tier="T1",
        source_url="https://news.example/direct", event_type="NEWS_WIRE",
        event_time="2026-11-12T01:00:00+00:00", headline="Nvidia board report",
        summary="", facts={"source": "Wire"}, status="UNVERIFIED",
        confidence=0.5, reasoning_version="v0",
    )
    return EventRecord(event, "2026-11-12T01:00:00+00:00", 60000)


def _base_context(as_of=CUTOFF):
    snapshot = build_event_snapshot(
        [_make_record()], since="2026-11-11T13:26:00+00:00", as_of=as_of,
    )
    return build_brief_context(snapshot, limit=48, target_universe=list(CASE_SYMBOLS))


def test_merge_appends_supplemental_facts_and_metadata(tmp_path):
    root = _write_root(tmp_path, {
        "NVDA": _raw("NVDA", collection_ts=ts(13, 14, 28), news=[{
            "title": "Nvidia grounded item",
            "publish_time": "2026-11-12T11:00:00Z",
            "url": "https://news.example/ok",
        }]),
    })
    result = extract_supplemental_evidence(root, cutoff=CUTOFF, case_symbols=("NVDA",))
    context = _base_context()
    base_count = len(context["facts"])
    merged = merge_supplemental_into_context(context, result, cutoff=CUTOFF)
    assert len(merged["facts"]) == base_count + 1
    assert merged["facts"][-1]["event_id"] == result["items"][0]["evidence_id"]
    assert merged["supplemental"]["merged_fact_count"] == 1
    assert merged["supplemental"]["evidence_hash"] == result["evidence_hash"]
    assert any("supplemental_evidence_from_da_raw_checkpoints" in item
               for item in merged["unknowns"])


def test_merge_discloses_supplemental_items_omitted_by_prompt_budget(tmp_path):
    root = _write_root(tmp_path, {
        "NVDA": _raw("NVDA", collection_ts=ts(13, 14, 28), news=[
            {"title": "Nvidia item one", "publish_time": "2026-11-12T11:00:00Z",
             "url": "https://news.example/one"},
            {"title": "Nvidia item two", "publish_time": "2026-11-12T11:01:00Z",
             "url": "https://news.example/two"},
        ]),
    })
    result = extract_supplemental_evidence(root, cutoff=CUTOFF, case_symbols=("NVDA",))
    merged = merge_supplemental_into_context(_base_context(), result, cutoff=CUTOFF, limit=1)
    assert merged["supplemental"]["merged_fact_count"] == 1
    assert merged["supplemental"]["omitted_fact_count"] == 1
    assert "supplemental_unreviewed_evidence_due_to_prompt_budget:1" in merged["unknowns"]


def test_merge_rejects_shared_cutoff_mismatch(tmp_path):
    root = _write_root(tmp_path, {
        "NVDA": _raw("NVDA", collection_ts=ts(13, 14, 28), news=[{
            "title": "Nvidia grounded item",
            "publish_time": "2026-11-12T11:00:00Z",
            "url": "https://news.example/ok",
        }]),
    })
    result = extract_supplemental_evidence(root, cutoff=CUTOFF, case_symbols=("NVDA",))
    context = _base_context(as_of="2026-11-12T13:15:00+00:00")
    with pytest.raises(ValueError, match="shared cutoff mismatch"):
        merge_supplemental_into_context(context, result, cutoff=CUTOFF)