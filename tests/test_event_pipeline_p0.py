import time
from datetime import datetime, timedelta

import yaml

import scripts.gen_event_symbols as gen_event_symbols
import scripts.llm_universe_scan as universe_scan
from app.catalyst.event_classifier import KEYWORD_EVENT_MAP, CatalystClassifier
from app.news_v2.models import NewsUrgency, UnifiedNewsItemV2


def _news(title: str) -> UnifiedNewsItemV2:
    return UnifiedNewsItemV2(
        id=f"test-{abs(hash(title))}",
        title=title,
        content="",
        source="test",
        source_name="test",
        timestamp=time.time(),
        urgency=NewsUrgency.NORMAL,
    )


def test_spacex_authorization_reaches_telecom_incumbents():
    classifier = CatalystClassifier(sector_mapping={"telecom": ["T", "TMUS", "VZ"]})
    events = classifier.classify(
        [_news(
            "SpaceX granted FCC authority to deploy 15,000 satellites "
            "for direct-to-cell service"
        )],
        use_llm=False,
    )

    assert len(events) == 1
    assert events[0].event_type == "REGULATORY_APPROVAL"
    assert set(events[0].tickers) == {"US.T", "US.TMUS", "US.VZ"}


def test_spectrum_acquisition_is_classified_and_propagates():
    classifier = CatalystClassifier(sector_mapping={"telecom": ["T", "TMUS", "VZ"]})
    events = classifier.classify(
        [_news("SpaceX acquires 800 MHz spectrum licenses from Grain Management")],
        use_llm=False,
    )

    assert len(events) == 1
    assert events[0].event_type == "SPECTRUM_TRANSACTION"
    assert set(events[0].tickers) == {"US.T", "US.TMUS", "US.VZ"}


def test_regulatory_event_map_contains_both_directions():
    directions = {
        direction
        for event_type, _severity, direction in KEYWORD_EVENT_MAP.values()
        if event_type.startswith("REGULATORY") and direction is not None
    }

    assert {"LONG", "SHORT"}.issubset(directions)


def test_production_config_contains_telecom_mapping():
    with open("config/unified_config.yaml", encoding="utf-8") as config_file:
        config = yaml.safe_load(config_file)

    assert set(config["catalyst"]["sector_mapping"]["telecom"]) == {"T", "TMUS", "VZ"}


def test_gen_event_symbols_uses_production_sector_mapping(monkeypatch):
    sector_mapping = {"telecom": ["T", "TMUS", "VZ"]}

    def fake_config(key, default=None):
        if key == "catalyst.sector_mapping":
            return sector_mapping
        return default

    monkeypatch.setattr(universe_scan, "_cfg", fake_config)
    monkeypatch.setattr(universe_scan, "_send_rsshub_alert", lambda content: None)
    monkeypatch.setattr(
        universe_scan,
        "_gather_news",
        lambda hours=24: [{
            "id": "test-spacex",
            "title": "SpaceX granted FCC authority for direct-to-cell service",
            "summary": "",
            "symbols": "[]",
        }],
    )

    result = gen_event_symbols.gather_hard_events()

    assert set(result) == {"US.T", "US.TMUS", "US.VZ"}


def test_stale_rsshub_empty_result_sends_alert(monkeypatch):
    from app.data import rsshub_us_news
    from app.observability import feishu_alert

    alerts = []

    class FakeFetcher:
        def get_recent_news(self, hours, limit):
            return []

        def get_last_fetched_at(self):
            return datetime.now() - timedelta(hours=7)

    class FakeAlerter:
        def __init__(self, app_id, app_secret):
            pass

        def send_warn(self, title, content, receive_id):
            alerts.append((title, content))

    monkeypatch.setattr(rsshub_us_news, "RSSHubUSNewsFetcher", FakeFetcher)
    monkeypatch.setattr(feishu_alert, "FeishuAlerter", FakeAlerter)

    def fake_config(key, default=None):
        if key == "universe_expansion.rsshub_stale_hours":
            return 6.0
        if key == "universe_expansion.clue_finnhub_news":
            return False
        return default

    monkeypatch.setattr(universe_scan, "_cfg", fake_config)

    result = universe_scan._gather_news(24)

    assert result == []
    assert len(alerts) == 1
    assert "RSSHub" in alerts[0][1]
    assert "0 条" in alerts[0][1]


def test_fresh_empty_rsshub_result_does_not_alert(monkeypatch):
    from app.data import rsshub_us_news
    from app.observability import feishu_alert

    alerts = []

    class FakeFetcher:
        def get_recent_news(self, hours, limit):
            return []

        def get_last_fetched_at(self):
            return datetime.now() - timedelta(hours=1)

    class FakeAlerter:
        def __init__(self, app_id, app_secret):
            pass

        def send_warn(self, title, content, receive_id):
            alerts.append((title, content))

    monkeypatch.setattr(rsshub_us_news, "RSSHubUSNewsFetcher", FakeFetcher)
    monkeypatch.setattr(feishu_alert, "FeishuAlerter", FakeAlerter)

    def fake_config(key, default=None):
        if key == "universe_expansion.rsshub_stale_hours":
            return 6.0
        if key == "universe_expansion.clue_finnhub_news":
            return False
        return default

    monkeypatch.setattr(universe_scan, "_cfg", fake_config)

    assert universe_scan._gather_news(24) == []
    assert alerts == []
