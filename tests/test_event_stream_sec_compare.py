"""SEC source shadow comparison tests (2026-10-18)."""
from types import SimpleNamespace

from scripts.event_stream_sec_compare import compare_results


def _event(url, event_type="SEC_8K_FILING", ticker="", items=None):
    return SimpleNamespace(
        source_url=url,
        event_type=event_type,
        facts={"ticker": ticker, "items": items or []},
    )


def test_compare_overlap_and_provider_item_coverage():
    raw = [_event("https://www.sec.gov/a", ticker="A")]
    provider = [
        _event("https://www.sec.gov/a", ticker="A", items=["1.01"]),
        _event("https://www.sec.gov/b", ticker="B"),
        _event("https://www.sec.gov/c", event_type="SEC_FORM4_FILING", ticker="C"),
    ]
    result = compare_results(raw, provider)
    assert result["raw_n"] == 1
    assert result["provider_n"] == 3
    assert result["url_overlap"] == 1
    assert result["raw_only"] == 0
    assert result["provider_only"] == 2
    assert result["provider_8k_n"] == 2
    assert result["provider_8k_with_items"] == 1
    assert result["provider_8k_item_coverage_pct"] == 50.0
    assert result["ticker_overlap"] == 1


def test_compare_empty_is_safe():
    result = compare_results([], [])
    assert result["raw_n"] == 0
    assert result["provider_n"] == 0
    assert result["url_overlap"] == 0
