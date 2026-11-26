from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import hashlib
import pytest

from scripts.da_input_freshness_manifest import _best_asof, _parse_ts, build_manifest


def test_manifest_discloses_asof_gap_and_fallback(tmp_path):
    day_dir = tmp_path / "2026-11-10"
    day_dir.mkdir()
    raw = {
        "symbol": "US.TEST",
        "saved_at": "2026-11-10T08:00:00-05:00",
        "collected": {
            "_quality": {"ok": 4, "total": 4},
            "_source_status": {"news": "ok"},
            "snapshot": {"session": "premarket", "update_time": "2026-11-10 07:59:00"},
            "market_context": {"vix": {"as_of_basis": "prev_close", "current": 15.0}},
            "capital_flow": {"net": 1.0},
            "news": [{"publish_time": "2026-11-10T07:00:00-05:00"}],
        },
    }
    (day_dir / "US.TEST.raw.json").write_text(json.dumps(raw), encoding="utf-8")
    manifest = build_manifest("2026-11-10", tmp_path)
    rows = {row["dimension"]: row for row in manifest["dimensions"]}
    assert manifest["raw_count"] == 1
    assert rows["snapshot"]["asof_n"] == 1
    assert rows["capital_flow"]["missing_n"] == 1
    assert rows["market_context"]["fallback_n"] == 1
    assert rows["news"]["asof_n"] == 1
    assert rows["snapshot"]["age_seconds_median"] == 60.0
    assert manifest["decision_use"] == "display_only_not_a_trading_gate"


def test_naive_timestamp_uses_et_not_utc():
    parsed = _parse_ts("2026-11-10 07:59:00")
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == -5 * 3600


def test_epoch_publish_time_produces_age(tmp_path):
    day_dir = tmp_path / "2026-11-10"
    day_dir.mkdir()
    saved = datetime(2026, 11, 10, 8, 0, tzinfo=ZoneInfo("America/New_York"))
    raw = {
        "symbol": "US.TEST",
        "saved_at": saved.isoformat(),
        "collected": {
            "news": [{"publish_time": str(int(saved.timestamp()) - 120)}],
        },
    }
    (day_dir / "US.TEST.raw.json").write_text(json.dumps(raw), encoding="utf-8")
    manifest = build_manifest("2026-11-10", tmp_path)
    news = next(row for row in manifest["dimensions"] if row["dimension"] == "news")
    assert news["asof_n"] == 1
    assert news["age_seconds_median"] == 120.0


def _inventory(tmp_path, collected, saved_at="2026-11-10T08:00:00-05:00"):
    day_dir = tmp_path / "2026-11-10"
    day_dir.mkdir(exist_ok=True)
    payload = {"symbol": "US.TEST", "collected": collected, "saved_at": saved_at}
    (day_dir / "US.TEST.raw.json").write_text(json.dumps(payload), encoding="utf-8")
    result = build_manifest("2026-11-10", tmp_path)
    return result, {row["dimension"]: row for row in result["dimensions"]}


@pytest.mark.parametrize("raw", ["not-a-time", "2026-11-10 07:59:00bad", "2026-11-10junk", "2026-02-30", True, float("nan"), "999999999999999999999"])
def test_invalid_clock_is_not_coverage(tmp_path, raw):
    assert _parse_ts(raw) is None
    assert _best_asof({"update_time": raw}) == (None, None)
    _, rows = _inventory(tmp_path, {"snapshot": {"update_time": raw}})
    assert rows["snapshot"]["asof_n"] == 0
    assert rows["snapshot"]["missing_n"] == 1
    assert rows["snapshot"]["invalid_clock_n"] == 1


def test_mixed_news_exposes_old_missing_invalid_and_future_records(tmp_path):
    _, rows = _inventory(tmp_path, {"news": [
        {"publish_time": "2026-11-10T07:59:00-05:00"},
        {"publish_time": "2026-10-01T08:00:00-04:00"},
        {"title": "unknown"}, {"publish_time": "invalid"},
        {"publish_time": "2026-11-11T08:00:00-05:00"},
    ]})
    row = rows["news"]
    assert row["record_n"] == 5
    assert row["timed_record_n"] == 2
    assert row["unknown_record_n"] == 3
    assert row["age_sample_n"] == 2
    assert row["age_seconds_min"] == 60
    assert row["age_seconds_max"] == 40 * 86400 + 3600
    assert row["future_information_clock_n"] == 1
    assert row["clock_roles"]["published"]["invalid_n"] == 1
    assert row["clock_roles"]["published"]["age_sample_n"] == 2


def test_collection_clock_does_not_wash_old_observation(tmp_path):
    _, rows = _inventory(tmp_path, {"snapshot": {
        "as_of": "2026-10-01T08:00:00-04:00", "timestamp": "2026-11-10T08:00:00-05:00",
    }})
    row = rows["snapshot"]
    assert row["age_seconds_max"] == 40 * 86400 + 3600
    assert row["clock_roles"]["collected"]["age_seconds_max"] == 0


def test_future_event_is_not_future_quote(tmp_path):
    _, rows = _inventory(tmp_path, {"earnings_calendar": {"next_earnings": {"date": "2026-11-27"}}})
    row = rows["earnings_calendar"]
    assert row["asof_n"] == 0
    assert row["future_information_clock_n"] == 0
    assert row["clock_roles"]["event_date"]["future_n"] == 1


def test_nested_market_clocks_remain_separate(tmp_path):
    _, rows = _inventory(tmp_path, {"market_context": {
        "vix": {"as_of_basis": "prev_close"},
        "spy": {"current_asof": "2026-11-09", "snapshot": {"snapshot_time": "2026-11-10 07:59:00"}},
        "qqq": {"current_asof": "2026-11-09", "snapshot": {"snapshot_time": "2026-11-10 07:58:00"}},
    }})
    row = rows["market_context"]
    assert row["record_n"] == 5
    assert row["timed_record_n"] == 4
    assert row["unknown_record_n"] == 1
    assert row["fallback_n"] == 1
    assert row["clock_roles"]["bar_date"]["age_seconds_max"] == 32 * 3600
    assert row["clock_roles"]["observed"]["age_seconds_max"] == 120


def test_peer_and_capital_flow_clocks_are_not_hidden_by_wrapper(tmp_path):
    _, rows = _inventory(tmp_path, {
        "industry_peers": {"peers": [{"price_as_of": "2026-11-10 07:55:00"}, {"symbol": "X"}]},
        "capital_flow": {"data": [{"last_valid_time": "2026-11-10 08:00:00", "capital_flow_item_time": "2026-11-09 09:30:00"}]},
    })
    assert rows["industry_peers"]["unknown_record_n"] == 1
    assert rows["industry_peers"]["age_seconds_max"] == 300
    assert rows["capital_flow"]["age_seconds_max"] == 22.5 * 3600
    assert rows["capital_flow"]["clock_roles"]["provider_clock"]["age_seconds_max"] == 0


def test_publication_clock_wins_over_partial_display_date(tmp_path):
    _, rows = _inventory(tmp_path, {"news_v2": [{"time": "11-10", "publish_time": "2026-11-10T07:59:00-05:00"}]})
    assert rows["news_v2"]["invalid_clock_n"] == 0
    assert rows["news_v2"]["age_seconds_max"] == 60


def test_missing_archive_time_is_not_replaced_by_collection_time(tmp_path):
    result, rows = _inventory(tmp_path, {"_timestamp": 1794315600.5, "news": [{"publish_time": "2026-11-10T07:59:00-05:00"}]}, saved_at=None)
    assert result["missing_archive_reference_n"] == 1
    assert rows["news"]["age_sample_n"] == 0
    assert _parse_ts(1794315600.5).timestamp() == 1794315600.5


def test_manifest_preserves_every_input_byte_and_count_invariants(tmp_path):
    _inventory(tmp_path, {"news": [{"publish_time": "2026-11-10T07:59:00-05:00"}, {}], "research": []})
    files = list(tmp_path.rglob("*.json"))
    before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    result = build_manifest("2026-11-10", tmp_path)
    assert before == {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    for row in result["dimensions"]:
        assert row["total_n"] == row["asof_n"] + row["missing_n"]
        assert row["record_n"] == row["timed_record_n"] + row["unknown_record_n"]
        for role in row["clock_roles"].values():
            assert role["clock_n"] == role["parsed_n"] + role["invalid_n"] + role["missing_n"]
    assert result["decision_use"] == "display_only_not_a_trading_gate"


@pytest.mark.parametrize("payload", [[], {"collected": []}, {"collected": {"_quality": "bad"}}])
def test_malformed_raw_shape_is_counted(tmp_path, payload):
    day = tmp_path / "2026-11-10"
    day.mkdir()
    (day / "bad.raw.json").write_text(json.dumps(payload))
    assert build_manifest("2026-11-10", tmp_path)["parse_error_n"] == 1


@pytest.mark.parametrize("value", [None, "", " "])
def test_null_source_clock_is_missing_not_malformed(tmp_path, value):
    _, rows = _inventory(tmp_path, {"industry_peers": {"peers": [{"price_as_of": value}]}})
    row = rows["industry_peers"]
    assert row["missing_clock_n"] == 1
    assert row["invalid_clock_n"] == 0
    assert row["unknown_record_n"] == 1
    assert row["asof_n"] == 0
    assert row["clock_roles"]["observed"]["missing_n"] == 1
