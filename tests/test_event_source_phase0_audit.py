import json

from scripts.audit_event_sources_phase0 import (
    _parse_finnhub,
    _parse_rss,
    _parse_sec_full_text,
    source_specs,
)


def test_source_specs_are_unique_and_read_only():
    specs = source_specs()
    assert len({spec["source_id"] for spec in specs}) == len(specs)
    assert all(spec["kind"] in {"sec_atom", "sec_full_text", "federal_register", "rss", "finnhub", "html", "pdf"} for spec in specs)


def test_parse_rss_extracts_sample_hits():
    xml = """<rss><channel>
      <item><title>SpaceX wins FCC approval</title><pubDate>Wed, 07 Oct 2026 02:06:27 GMT</pubDate></item>
      <item><title>Other news</title><pubDate>Wed, 07 Oct 2026 03:00:00 GMT</pubDate></item>
    </channel></rss>"""
    parsed = _parse_rss(xml)
    assert parsed["result_count"] == 2
    assert parsed["first_title"] == "SpaceX wins FCC approval"
    assert parsed["first_timestamp"] == "Wed, 07 Oct 2026 02:06:27 GMT"
    assert parsed["sample_hits"] == ["SpaceX wins FCC approval"]


def test_parse_sec_full_text_counts_total_hits():
    payload = {
        "hits": {
            "total": {"value": 39},
            "hits": [{"_source": {
                "display_names": ["Example Trust"],
                "file_date": "2026-10-08",
                "form": "N-CSR",
            }}],
        }
    }
    parsed = _parse_sec_full_text(json.dumps(payload))
    assert parsed["result_count"] == 39
    assert parsed["first_title"] == "Example Trust"
    assert parsed["first_timestamp"] == "2026-10-08"


def test_parse_finnhub_redacts_nothing_but_converts_timestamps():
    payload = json.dumps([{
        "headline": "SpaceX update",
        "source": "CNBC",
        "datetime": 1791404688,
        "url": "https://example.com",
    }])
    parsed = _parse_finnhub(payload)
    assert parsed["result_count"] == 1
    assert parsed["first_timestamp"].startswith("2026-10-07T")
    assert parsed["sample_hits"][0]["source"] == "CNBC"
