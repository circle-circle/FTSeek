from __future__ import annotations

import logging
import time

import requests

from app.data.twelvedata_provider import TwelveDataProvider


def _http_error(status_code: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status_code
    return requests.HTTPError(response=response)


def test_http_429_sets_breaker_and_logs_warning(monkeypatch, caplog):
    provider = TwelveDataProvider(api_key="test-key")
    calls = []

    def fake_get(*args, **kwargs):
        calls.append((args, kwargs))
        raise _http_error(429)

    monkeypatch.setattr(requests, "get", fake_get)

    with caplog.at_level(logging.WARNING, logger="app.data.twelvedata_provider"):
        assert provider._get("time_series", {"symbol": "TEST"}) is None

    assert len(calls) == 1
    assert provider._rate_limited_until > time.monotonic()
    assert any(record.levelno == logging.WARNING and "429" in record.message for record in caplog.records)
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)

    assert provider._get("time_series", {"symbol": "TEST2"}) is None
    assert len(calls) == 1


def test_non_429_http_error_keeps_error_and_no_breaker(monkeypatch, caplog):
    provider = TwelveDataProvider(api_key="test-key")

    def fake_get(*args, **kwargs):
        raise _http_error(500)

    monkeypatch.setattr(requests, "get", fake_get)

    with caplog.at_level(logging.WARNING, logger="app.data.twelvedata_provider"):
        assert provider._get("time_series", {"symbol": "TEST"}) is None

    assert provider._rate_limited_until == 0.0
    assert any(record.levelno == logging.ERROR for record in caplog.records)
