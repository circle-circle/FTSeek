from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import pytest
import requests

from app.data.fred_series import latest_valid_observation


class _Response:
    def __init__(self, text="", payload=None, status_code=200):
        self.text = text
        self._payload = payload or {}
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


def _monitor():
    from app.risk.vix_monitor import VIXMonitor

    monitor = VIXMonitor.__new__(VIXMonitor)
    monitor._cache = {}
    monitor._last_as_of = None
    return monitor


@pytest.mark.parametrize("csv_text,expected", [
    ("observation_date,VIXCLS\n2026-11-05,28.18\n2026-11-06,25.64\n2026-11-07,.\n",
     ("2026-11-06", 25.64)),
    ("observation_date,VIXCLS\n2026-11-05,nan\n2026-11-04,not-a-date\n", (None, None)),
    ("", (None, None)),
])
def test_latest_valid_fred_observation(csv_text, expected):
    assert latest_valid_observation(csv_text) == expected


def test_alphavantage_fred_uses_last_valid_observation(monkeypatch):
    from app.data import alphavantage_provider as avp

    csv_text = "observation_date,VIXCLS\n2026-11-05,28.18\n2026-11-06,25.64\n2026-11-07,.\n"
    monkeypatch.setattr(avp.requests, "get", lambda *args, **kwargs: _Response(csv_text))
    provider = avp.AlphaVantageProvider.__new__(avp.AlphaVantageProvider)
    provider.cache = {}
    provider.cache_time = {}

    assert provider._fetch_fred_vix("test") == pytest.approx(25.64)
    assert provider.cache["test"] == {"vix": 25.64, "source": "fred", "as_of": "2026-11-06"}


def test_vix_monitor_alphavantage_records_trading_day(monkeypatch):
    from app.risk import vix_monitor as vm

    monitor = _monitor()
    monitor.api_key = "key"
    payload = {"Global Quote": {"05. price": "25.64", "07. latest trading day": "2026-11-06"}}
    monkeypatch.setattr(vm.requests, "get", lambda *args, **kwargs: _Response(payload=payload))

    assert monitor._fetch_vix_alphavantage() == pytest.approx(25.64)
    assert monitor._last_as_of == "2026-11-06"


def test_vix_monitor_finnhub_and_yahoo_record_observation_date(monkeypatch):
    from app.risk import vix_monitor as vm

    timestamp = datetime(2026, 11, 6, 21, tzinfo=timezone.utc).timestamp()
    monkeypatch.setenv("FINNHUB_API_KEY", "key")
    monkeypatch.setattr(vm.requests, "get", lambda *args, **kwargs: _Response(payload={"c": 20.0, "t": timestamp}))
    monitor = _monitor()
    assert monitor._fetch_vix_finnhub() == pytest.approx(17.0)
    assert monitor._last_as_of == "2026-11-06"

    monkeypatch.setattr(vm.requests, "get", lambda *args, **kwargs: _Response(payload={"c": 20.0, "t": 0}))
    monitor = _monitor()
    assert monitor._fetch_vix_finnhub() == pytest.approx(17.0)
    assert monitor._last_as_of is None

    payload = {"chart": {"result": [{"meta": {"regularMarketPrice": 25.64, "regularMarketTime": timestamp}}]}}
    monkeypatch.setattr(vm.requests, "get", lambda *args, **kwargs: _Response(payload=payload))
    monitor = _monitor()
    assert monitor._fetch_vix_fallback() == pytest.approx(25.64)
    assert monitor._last_as_of == "2026-11-06"


def test_term_structure_requires_matching_observation_dates(monkeypatch):
    from app.risk import vix_monitor as vm

    monitor = _monitor()

    def spot():
        monitor._last_as_of = "2026-11-06"
        return 25.0

    monkeypatch.setattr(monitor, "_fetch_vix_fred", spot)
    vxv = "observation_date,VXVCLS\n2026-11-06,26.0\n2026-11-07,.\n"
    monkeypatch.setattr(vm.requests, "get", lambda *args, **kwargs: _Response(vxv))
    result = monitor.get_term_structure()
    assert result["spread_pct"] == pytest.approx(4.0)
    assert result["spot_as_of"] == result["vxv_as_of"] == "2026-11-06"

    mismatched = "observation_date,VXVCLS\n2026-11-05,26.0\n2026-11-06,.\n"
    monkeypatch.setattr(vm.requests, "get", lambda *args, **kwargs: _Response(mismatched))
    result = monitor.get_term_structure()
    assert result["status"] == "mismatched_observation_date"
    assert result["spread_pct"] is None
    assert result["regime"] == "unknown"
    assert result["adjustment"] == 1.0


def test_dynamic_economic_calendar_renders_source_and_failure_truthfully(monkeypatch):
    from app.data import premarket_enrichment as pm
    from scripts import macro_calendar_shadow_collect as calendar

    monkeypatch.setattr(calendar, "_fetch_futu_event_schedule", lambda now: {
        "status": "ok",
        "events": [{"title": "CPI", "ts_et": "2026-11-10 08:30", "star": "HIGH"}],
    })
    text = pm._get_economic_calendar()
    assert "title=CPI" in text
    assert "ts_et=2026-11-10 08:30 ET" in text
    assert "star=HIGH" in text
    assert "source=Futu/get_economic_calendar" in text

    monkeypatch.setattr(calendar, "_fetch_futu_event_schedule", lambda now: {"status": "error: timeout"})
    text = pm._get_economic_calendar()
    assert "Economic calendar unavailable" in text
    assert "No major scheduled events" not in text
    assert "do not infer no events" in text


def _fred_responses(same_date=True):
    date_2y = "2026-11-05" if same_date else "2026-11-06"
    return {
        "DGS10": f"observation_date,DGS10\n2026-11-05,4.10\n2026-11-06,.\n",
        "DGS2": f"observation_date,DGS2\n{date_2y},3.50\n2026-11-07,.\n",
        "DGS5": "observation_date,DGS5\n2026-11-05,3.80\n2026-11-06,.\n",
        "VIXCLS": "observation_date,VIXCLS\n2026-11-05,25.64\n2026-11-06,.\n",
        "DFF": "observation_date,DFF\n2026-11-05,3.64\n2026-11-06,.\n",
    }


def test_treasury_series_have_dates_and_same_day_spread(monkeypatch):
    from app.data import premarket_enrichment as pm

    responses = _fred_responses()

    def get(url, **kwargs):
        series = parse_qs(urlparse(url).query)["id"][0]
        return _Response(responses[series])

    monkeypatch.setattr(requests, "get", get)
    result = pm._collect_treasury_yields()
    assert result["VIX"] == 25.64
    assert result["as_of"]["VIX"] == "2026-11-05"
    assert result["spread_10y_2y"] == pytest.approx(0.6)
    rendered = pm.format_for_debate({"treasury_yields": result})
    assert "VIX: 25.64 (FRED close, as of 2026-11-05)" in rendered
    assert "10Y-2Y Spread: 0.6%" in rendered


def test_treasury_spread_is_na_for_mismatched_dates(monkeypatch):
    from app.data import premarket_enrichment as pm

    responses = _fred_responses(same_date=False)

    def get(url, **kwargs):
        series = parse_qs(urlparse(url).query)["id"][0]
        return _Response(responses[series])

    monkeypatch.setattr(requests, "get", get)
    result = pm._collect_treasury_yields()
    assert result["spread_10y_2y"] is None
    assert "observation dates differ" in result["curve_shape"]
    assert "10Y-2Y Spread: N/A" in pm.format_for_debate({"treasury_yields": result})


def test_position_sync_none_vix_preserves_existing_neutral_factor(monkeypatch):
    from app.core.position_sync import USPositionSync
    from app.data import alphavantage_provider as avp

    provider = type("Provider", (), {"get_vix_or_none": lambda self: None})()
    monkeypatch.setattr(avp, "get_alpha_vantage_provider", lambda: provider)
    sync = USPositionSync.__new__(USPositionSync)
    assert sync._get_vix_factor() == 1.0


def test_llm_vix_missing_is_na_instead_of_normal_market():
    from app.pipeline.llm_pipeline import _generate_degraded_recommendation, _vix_prompt_text

    assert "N/A" in _vix_prompt_text(None)
    text = _generate_degraded_recommendation([], {"vix": None}, [])
    assert "VIX=N/A" in text
    assert "不据此判断市场波动" in text
    assert "VIX=20.0" not in text


def test_llm_vix_getter_rejects_invalid_sentinel_values():
    from types import SimpleNamespace
    from app.pipeline.llm_pipeline import _market_vix_or_none

    scheduler = SimpleNamespace(market_data=SimpleNamespace(get_vix_or_none=lambda: float("nan")))
    assert _market_vix_or_none(scheduler) is None
    scheduler.market_data.get_vix_or_none = lambda: "28.18"
    assert _market_vix_or_none(scheduler) == 28.18


def test_regime_prompt_does_not_classify_missing_vix():
    from app.risk.market_regime_filter import MarketRegimeFilter

    regime_filter = MarketRegimeFilter.__new__(MarketRegimeFilter)
    result = regime_filter.get_regime_summary({}, None)
    assert result["regime"] == "UNKNOWN"
    assert result["vix"] is None
    assert result["vix_level"] == "不可用"


def test_quality_gate_missing_vix_keeps_legacy_score_without_fake_observation():
    from app.factors.regime_filter import RegimeFilter

    regime_filter = RegimeFilter.__new__(RegimeFilter)
    regime_filter.enabled = True
    regime_filter.max_vix = 30.0
    regime_filter.spy_trend_ma = 50
    regime_filter.bear_penalty = 0.5
    result = regime_filter.check("TEST", {}, {"vix": None, "vix_score_fallback": 18.0})
    assert result.passed is True
    assert result.score == pytest.approx(0.9)
    assert result.metadata["vix_observed"] is False
    assert "非观测值" in result.reason
    assert "市场环境正常 (VIX=18.0)" not in result.reason

    observed = regime_filter.check("TEST", {}, {"vix": 18.0})
    assert observed.score == result.score
    assert observed.metadata["vix_observed"] is True
