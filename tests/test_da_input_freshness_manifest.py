from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from scripts.da_input_freshness_manifest import _parse_ts, build_manifest


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
