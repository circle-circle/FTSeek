from __future__ import annotations

import json

import pytest

from app.event_stream.ingest_service import IngestService
from app.event_stream.models import CanonicalEvent, RawEvent, SourceResult, make_event_id
from app.event_stream.dedupe import deduplicate_events
from app.event_stream.relevance import RelevanceContext, filter_relevant_events
from app.event_stream.source_registry import default_source_registry
from app.event_stream.store import EventStore
from app.event_stream.sources.federal_register import parse_federal_register
from app.event_stream.sources.finnhub_news import parse_finnhub_news
from app.event_stream.sources.google_news_rss import parse_google_news_rss
from app.event_stream.sources.sec_edgar_atom import parse_sec_edgar_atom
from app.event_stream.ue_clue_adapter import build_clue_text, gather_event_stream_clue


def _raw_event() -> RawEvent:
    return RawEvent(
        raw_id="raw-1",
        source_id="TEST_SOURCE",
        source_tier="T0",
        source_url="https://example.com",
        source_published_at="2026-10-09T00:00:00Z",
        fetched_at="2026-10-09T01:00:00Z",
        payload_hash="hash-1",
        raw_artifact="payload",
        parse_status="PARSED",
    )


def _event(event_id: str = "event-1") -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        source_id="TEST_SOURCE",
        source_tier="T0",
        source_url="https://example.com",
        event_type="TEST_EVENT",
        event_time="2026-10-09T00:00:00Z",
        headline="Test headline",
        summary="Test summary",
        facts={"foo": "bar"},
        status="UNVERIFIED",
        confidence=0.5,
        reasoning_version="v0",
    )


def test_event_store_is_append_only_and_idempotent(tmp_path):
    store = EventStore(tmp_path / "event_stream.db")
    assert store.append_raw_event(_raw_event()) is True
    assert store.append_raw_event(_raw_event()) is False
    assert store.append_canonical_event(_event()) is True
    assert store.append_canonical_event(_event()) is False
    assert store.get_recent_canonical_events() == [_event()]


def test_event_store_source_health_roundtrip(tmp_path):
    store = EventStore(tmp_path / "event_stream.db")
    store.mark_source_health("TEST_SOURCE", "OK", 123, "2026-10-09T00:00:00Z")
    store.mark_source_health("TEST_SOURCE", "ERROR", 456, None, "boom")
    health = store.get_source_health("TEST_SOURCE")
    assert health["status"] == "ERROR"
    assert health["latency_ms"] == 456
    assert health["last_success_at"] == "2026-10-09T00:00:00Z"
    assert health["last_error"] == "boom"


def test_event_store_records_first_seen_latency_once(tmp_path):
    db_path = tmp_path / "event_stream.db"
    store = EventStore(db_path)
    assert store.append_canonical_event(
        _event(),
        first_seen_at="2026-10-09T00:10:00+00:00",
    )
    assert not store.append_canonical_event(
        _event(),
        first_seen_at="2026-10-09T00:20:00+00:00",
    )
    stats = store.get_latency_stats()
    assert stats == [{
        "source_id": "TEST_SOURCE",
        "events_with_latency": 1,
        "avg_published_to_seen_ms": 600000,
        "min_published_to_seen_ms": 600000,
        "max_published_to_seen_ms": 600000,
    }]


def test_event_store_parses_google_rfc822_event_time(tmp_path):
    db_path = tmp_path / "event_stream.db"
    store = EventStore(db_path)
    event = _event()
    event.event_time = "Wed, 07 Oct 2026 02:06:27 GMT"
    assert store.append_canonical_event(
        event,
        first_seen_at="2026-10-07T02:06:37+00:00",
    )
    stats = store.get_latency_stats()
    assert stats[0]["events_with_latency"] == 1
    assert stats[0]["avg_published_to_seen_ms"] == 10000


def test_event_store_backfills_missing_latency_metadata(tmp_path):
    db_path = tmp_path / "event_stream.db"
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """CREATE TABLE canonical_events (
                event_id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL,
                source_tier TEXT NOT NULL,
                source_url TEXT NOT NULL,
                event_type TEXT NOT NULL,
                event_time TEXT,
                headline TEXT NOT NULL,
                summary TEXT,
                facts_json TEXT NOT NULL,
                status TEXT NOT NULL,
                confidence REAL NOT NULL,
                reasoning_version TEXT NOT NULL,
                first_seen_at TEXT,
                published_to_seen_ms INTEGER
            )"""
        )
        conn.execute(
            """INSERT INTO canonical_events VALUES (
                'event-1', 'TEST_SOURCE', 'T0', 'https://example.com', 'TEST_EVENT',
                'Wed, 07 Oct 2026 02:06:27 GMT', 'Test headline', 'Test summary', '{}',
                'UNVERIFIED', 0.5, 'v0', '2026-10-07T02:06:37+00:00', NULL
            )"""
        )

    store = EventStore(db_path)
    stats = store.get_latency_stats()
    assert stats[0]["events_with_latency"] == 1
    assert stats[0]["avg_published_to_seen_ms"] == 10000


def test_event_store_migrates_existing_phase1_schema(tmp_path):
    db_path = tmp_path / "event_stream.db"
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """CREATE TABLE canonical_events (
                event_id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL,
                source_tier TEXT NOT NULL,
                source_url TEXT NOT NULL,
                event_type TEXT NOT NULL,
                event_time TEXT,
                headline TEXT NOT NULL,
                summary TEXT,
                facts_json TEXT NOT NULL,
                status TEXT NOT NULL,
                confidence REAL NOT NULL,
                reasoning_version TEXT NOT NULL
            )"""
        )

    store = EventStore(db_path)
    assert store.append_canonical_event(_event())
    assert store.get_counts()["canonical_events"] == 1


def test_parse_sec_edgar_atom():
    # 2026-10-09 F2: fixture 改为 SEC 真实 Atom 形态 — <link> 的 URL 在 href 属性,
    #   旧 fixture 用文本形态掩盖了生产库 52/52 source_url 全空的 bug。
    xml = """<feed xmlns="http://www.w3.org/2005/Atom">
      <entry><title>8-K - TEST</title>
      <link rel="alternate" type="text/html" href="https://sec.gov/test"/>
      <updated>2026-10-09T07:10:32-04:00</updated><summary>Test filing</summary></entry>
    </feed>"""
    events = parse_sec_edgar_atom(xml)
    assert len(events) == 1
    assert events[0].event_type == "SEC_8K_FILING"
    assert events[0].headline == "8-K - TEST"
    assert events[0].event_time == "2026-10-09T07:10:32-04:00"
    assert events[0].source_url == "https://sec.gov/test"


def test_parse_federal_register():
    payload = json.dumps({
        "results": [{
            "title": "SpaceX notice",
            "html_url": "https://example.gov/doc",
            "publication_date": "2026-09-29",
            "abstract": "notice abstract",
            "document_number": "2026-0001",
        }]
    })
    events = parse_federal_register(payload)
    assert len(events) == 1
    assert events[0].event_type == "FEDERAL_REGISTER_NOTICE"
    assert events[0].facts["document_number"] == "2026-0001"


def test_parse_google_news_rss():
    xml = """<rss><channel>
      <item><title>SpaceX FCC</title><link>https://news.example</link>
      <pubDate>Wed, 07 Oct 2026 02:06:27 GMT</pubDate>
      <description>description</description><source>PCMag</source></item>
    </channel></rss>"""
    events = parse_google_news_rss(xml, query="FCC OR FTC")
    assert len(events) == 1
    assert events[0].event_type == "NEWS_DISCOVERY"
    assert events[0].source_tier == "T2"
    assert events[0].facts["source"] == "PCMag"
    assert events[0].facts["query"] == "FCC OR FTC"


def test_parse_finnhub_news():
    payload = json.dumps([{
        "headline": "SpaceX update",
        "url": "https://example.com",
        "datetime": 1791404688,
        "summary": "summary",
        "source": "CNBC",
    }])
    events = parse_finnhub_news(payload)
    assert len(events) == 1
    assert events[0].event_type == "NEWS_WIRE"
    assert events[0].event_time == "2026-10-07T20:24:48+00:00"


def test_default_source_registry_contains_phase0_sources():
    registry = default_source_registry()
    assert set(registry) == {
        "SEC_EDGAR_8K_ATOM",
        "FEDERAL_REGISTER_API",
        "GOOGLE_NEWS_RSS",
        "FINNHUB_GENERAL_NEWS",
    }


def test_ingest_service_stores_raw_and_events(tmp_path):
    store = EventStore(tmp_path / "event_stream.db")
    raw = _raw_event()
    event = _event()

    def fake_fetch() -> SourceResult:
        return SourceResult(
            source_id="TEST_SOURCE",
            source_tier="T0",
            status="OK",
            latency_ms=12,
            raw_event=raw,
            events=[event],
        )

    service = IngestService(store, sources={"TEST_SOURCE": fake_fetch})
    summary = service.ingest()
    assert summary["TEST_SOURCE"]["status"] == "OK"
    assert summary["TEST_SOURCE"]["events_inserted"] == 1
    assert store.get_recent_canonical_events() == [event]


def test_ingest_service_preserves_registry_result_order(tmp_path):
    """2026-10-17 审计: 结果按注册顺序处理 (T0 在前), 抓取本身并发。"""
    store = EventStore(tmp_path / "event_stream.db")
    calls = []

    def _fetcher(source_id):
        def _fetch() -> SourceResult:
            calls.append(source_id)
            return SourceResult(
                source_id=source_id, source_tier="T0", status="OK", latency_ms=1)

        return _fetch

    service = IngestService(
        store,
        sources={
            "SEC_EDGAR_8K_ATOM": _fetcher("SEC_EDGAR_8K_ATOM"),
            "GOOGLE_NEWS_RSS": _fetcher("GOOGLE_NEWS_RSS"),
        },
    )
    summary = service.ingest()
    assert set(calls) == {"SEC_EDGAR_8K_ATOM", "GOOGLE_NEWS_RSS"}
    assert list(summary.keys()) == ["SEC_EDGAR_8K_ATOM", "GOOGLE_NEWS_RSS"]
    assert service.last_total_latency_ms is not None


def test_ingest_service_fetches_sources_concurrently(tmp_path):
    """慢源不得串行阻塞另一源; 两个 fetcher 必须能同时进入."""
    import threading

    store = EventStore(tmp_path / "event_stream.db")
    barrier = threading.Barrier(2, timeout=5)

    def _fetcher(source_id):
        def _fetch() -> SourceResult:
            barrier.wait()
            return SourceResult(
                source_id=source_id, source_tier="T0", status="OK", latency_ms=1)

        return _fetch

    service = IngestService(
        store,
        sources={
            "SEC_EDGAR_8K_ATOM": _fetcher("SEC_EDGAR_8K_ATOM"),
            "GOOGLE_NEWS_RSS": _fetcher("GOOGLE_NEWS_RSS"),
        },
    )
    summary = service.ingest()
    assert summary["SEC_EDGAR_8K_ATOM"]["status"] == "OK"
    assert summary["GOOGLE_NEWS_RSS"]["status"] == "OK"


def test_ingest_service_isolates_source_exception(tmp_path):
    store = EventStore(tmp_path / "event_stream.db")

    def _boom() -> SourceResult:
        raise RuntimeError("boom")

    def _ok() -> SourceResult:
        return SourceResult(
            source_id="GOOGLE_NEWS_RSS", source_tier="T2", status="OK", latency_ms=1)

    service = IngestService(
        store, sources={"SEC_EDGAR_8K_ATOM": _boom, "GOOGLE_NEWS_RSS": _ok})
    summary = service.ingest()
    assert summary["SEC_EDGAR_8K_ATOM"]["status"] == "ERROR"
    assert "boom" in summary["SEC_EDGAR_8K_ATOM"]["error"]
    assert summary["GOOGLE_NEWS_RSS"]["status"] == "OK"
    assert store.get_source_health("SEC_EDGAR_8K_ATOM")["status"] == "ERROR"


def test_ingest_service_marks_partial_source_health(tmp_path):
    """部分 query 失败不得再标 OK + last_error=None。"""
    store = EventStore(tmp_path / "event_stream.db")
    raw = _raw_event()
    event = _event()

    def fake_fetch() -> SourceResult:
        return SourceResult(
            source_id="TEST_SOURCE",
            source_tier="T0",
            status="OK",
            latency_ms=12,
            raw_event=raw,
            events=[event],
            error="[spectrum] Timeout: boom",
        )

    service = IngestService(store, sources={"TEST_SOURCE": fake_fetch})
    summary = service.ingest()
    assert summary["TEST_SOURCE"]["status"] == "PARTIAL"
    assert summary["TEST_SOURCE"]["partial"] is True
    health = store.get_source_health("TEST_SOURCE")
    assert health["status"] == "PARTIAL"
    assert "boom" in health["last_error"]
    assert health["last_success_at"] is not None


def test_event_store_reads_by_first_seen_window(tmp_path):
    """2026-10-17 审计: 时间窗查询不得被 rowid 行数截断。"""
    store = EventStore(tmp_path / "event_stream.db")
    old = _event("old")
    old.headline = "Old event"
    store.append_canonical_event(old, first_seen_at="2026-10-08T00:00:00+00:00")
    recent = _event("recent")
    recent.headline = "Recent event"
    store.append_canonical_event(recent, first_seen_at="2026-10-09T12:00:00+00:00")

    events = store.get_canonical_events_since("2026-10-09T00:00:00+00:00")
    assert [event.event_id for event in events] == ["recent"]
    assert [event.event_id for event in store.get_canonical_events_since(
        "2026-10-09T00:00:00+00:00", limit=1)] == ["recent"]



def test_build_clue_text_formats_events_for_ue():
    text = build_clue_text([_event()])
    assert "[T0/TEST_EVENT] 2026-10-09T00:00:00Z | Test headline" in text
    assert "https://example.com" in text


def test_gather_event_stream_clue_reads_store(tmp_path):
    store = EventStore(tmp_path / "event_stream.db")
    store.append_canonical_event(_event())
    text = gather_event_stream_clue(store_path=tmp_path / "event_stream.db")
    assert "Test headline" in text


def test_relevance_filter_matches_telecom_competitor_event():
    event = _event()
    event.headline = "SpaceX receives FCC authority for direct-to-cell satellites"
    context = RelevanceContext(
        symbol_names={"T": "AT&T", "TMUS": "T-Mobile", "VZ": "Verizon"},
        sector_symbols={"telecom": ["T", "TMUS", "VZ"]},
    )
    assert filter_relevant_events([event], context) == [event]


def test_relevance_filter_rejects_unrelated_general_news():
    event = _event()
    event.headline = "Local marathon raises record donations"
    context = RelevanceContext(symbol_names={"T": "AT&T"}, sector_symbols={"telecom": ["T"]})
    assert filter_relevant_events([event], context) == []


def test_relevance_filter_requires_uppercase_ticker():
    event = _event()
    event.headline = "Ryanair says jet fuel cost could remain higher"
    context = RelevanceContext(symbol_names={"COST": "Costco"}, sector_symbols={})
    assert filter_relevant_events([event], context) == []


def test_relevance_ignores_publisher_suffix_and_query_facts():
    """2026-10-17 审计: "Spectrum News 13" 发布方 + spectrum 查询词不得命中 telecom。"""
    event = _event()
    event.headline = (
        "5 things to know about Kings Entertainment's Spooky Takeover"
        " - Spectrum News 13"
    )
    event.summary = (
        '<a href="https://news.google.com/rss/articles/xyz" target="_blank">'
        "5 things to know about Kings Entertainment's Spooky Takeover</a>"
        '&nbsp;&nbsp;<font color="#6f6f6f">Spectrum News 13</font>'
    )
    event.facts = {
        "source": "Spectrum News 13",
        "query": "spectrum OR direct-to-cell OR satellite broadband",
    }
    context = RelevanceContext(
        symbol_names={},
        sector_symbols={"telecom": ["T", "TMUS", "VZ"]},
    )
    assert filter_relevant_events([event], context) == []


def test_relevance_ignores_query_facts_without_headline_match():
    event = _event()
    event.headline = "Local marathon raises record donations"
    event.summary = ""
    event.facts = {"query": "spectrum OR direct-to-cell"}
    context = RelevanceContext(symbol_names={}, sector_symbols={"telecom": ["T"]})
    assert filter_relevant_events([event], context) == []


def test_relevance_still_matches_after_stripping_publisher_suffix():
    event = _event()
    event.headline = "Spectrum deal expands wireless carrier coverage - PCMag"
    event.summary = ""
    event.facts = {"source": "PCMag"}
    context = RelevanceContext(symbol_names={}, sector_symbols={"telecom": ["T"]})
    assert filter_relevant_events([event], context) == [event]


def test_relevance_does_not_strip_plain_summary_company_name():
    """纯文本摘要末尾与 source 同名时不得误削 (只有 HTML 尾注才剥)."""
    event = _event()
    event.headline = "New product launch announced"
    event.summary = "The company discussed is Apple"
    event.facts = {"source": "Apple"}
    context = RelevanceContext(
        symbol_names={"AAPL": "Apple"}, sector_symbols={})
    assert filter_relevant_events([event], context) == [event]



def test_cross_source_dedupe_prefers_t0_primary():
    primary = _event("official")
    primary.source_tier = "T0"
    primary.source_id = "SEC"
    primary.source_url = "https://sec.gov/filing"
    primary.headline = "Company files 8-K for spectrum transaction"
    primary.event_time = "2026-10-09T10:00:00+00:00"

    duplicate = _event("news")
    duplicate.source_tier = "T2"
    duplicate.source_id = "GOOGLE"
    duplicate.source_url = "https://news.example/company-spectrum"
    duplicate.headline = "Company files 8-K for spectrum deal"
    duplicate.event_time = "2026-10-09T10:10:00+00:00"

    clusters = deduplicate_events([duplicate, primary])
    assert len(clusters) == 1
    assert clusters[0].primary is primary
    assert clusters[0].duplicates == [duplicate]


# ── 2026-10-09 §22 修复批 (F1/F5/F6) ─────────────────────────


def _rss_xml(title: str, link: str) -> str:
    return f"""<rss><channel>
      <item><title>{title}</title><link>{link}</link>
      <pubDate>Thu, 09 Oct 2026 12:00:00 GMT</pubDate>
      <description>d</description><source>S</source></item>
    </channel></rss>"""


def test_google_fetch_merges_multiple_queries(monkeypatch):
    """F1: 多 query 合并 + 单 query 失败不拖垮整源 + event_id 去重。"""
    from app.event_stream.sources import google_news_rss as mod

    calls = []

    class _Resp:
        def __init__(self, text):
            self.text = text
            self.content = text.encode("utf-8")

        def raise_for_status(self):
            return None

    def fake_get(url, headers=None, timeout=None):
        calls.append(url)
        if "FAIL" in url:
            raise RuntimeError("boom")
        if "spectrum" in url:
            return _Resp(_rss_xml("Spectrum deal", "https://news.example/1"))
        return _Resp(_rss_xml("FCC action", "https://news.example/2"))

    monkeypatch.setattr(mod.requests, "get", fake_get)
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
    result = mod.fetch_google_news_rss(queries=["spectrum", "FCC", "FAIL"])
    assert result.status == "OK"
    assert len(calls) == 3
    assert {e.headline for e in result.events} == {"Spectrum deal", "FCC action"}
    assert result.error and "FAIL" in result.error
    assert {e.facts["query"] for e in result.events} == {"spectrum", "FCC"}


def test_google_fetch_all_queries_fail_returns_error(monkeypatch):
    from app.event_stream.sources import google_news_rss as mod

    def fake_get(url, headers=None, timeout=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(mod.requests, "get", fake_get)
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
    result = mod.fetch_google_news_rss(queries=["a", "b"])
    assert result.status == "ERROR"
    assert result.events == []


def test_federal_register_merges_multiple_terms(monkeypatch):
    """F1: 多 term 合并; document_number 去重 (event_id 同源同 url 去重)。"""
    from app.event_stream.sources import federal_register as mod

    payloads = {
        "alpha": {"results": [{"title": "Doc A", "html_url": "https://fr.example/a",
                               "publication_date": "2026-10-09", "document_number": "1"}]},
        "beta": {"results": [{"title": "Doc B", "html_url": "https://fr.example/b",
                              "publication_date": "2026-10-09", "document_number": "2"}]},
    }

    class _Resp:
        def __init__(self, text):
            self.text = text
            self.content = text.encode("utf-8")

        def raise_for_status(self):
            return None

    def fake_get(url, headers=None, timeout=None):
        for key, payload in payloads.items():
            if key in url:
                return _Resp(json.dumps(payload))
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(mod.requests, "get", fake_get)
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
    result = mod.fetch_federal_register(terms=["alpha", "beta"])
    assert result.status == "OK"
    assert {e.headline for e in result.events} == {"Doc A", "Doc B"}
    assert {e.facts["term"] for e in result.events} == {"alpha", "beta"}


def test_configured_queries_fall_back_to_defaults():
    from app.event_stream.sources import federal_register as fr_mod
    from app.event_stream.sources import google_news_rss as gn_mod

    # 读生产 config (本仓已登记 event_stream.google_news_queries) 或回退默认;
    # 两种形态都必须是非空字符串列表。
    for queries in (gn_mod._configured_queries(), fr_mod._configured_terms()):
        assert isinstance(queries, list) and queries
        assert all(isinstance(q, str) and q.strip() for q in queries)


def test_dedupe_missing_time_does_not_merge():
    """F5: 时间缺失时不得仅按标题相似合并 (宁可漏合并)。"""
    left = _event("a")
    left.headline = "Company files 8-K for spectrum transaction"
    left.source_url = "https://a.example/1"
    left.event_time = None
    right = _event("b")
    right.headline = "Company files 8-K for spectrum deal"
    right.source_url = "https://b.example/2"
    right.event_time = None
    clusters = deduplicate_events([left, right])
    assert len(clusters) == 2


def test_dedupe_naive_date_and_aware_time_do_not_crash():
    """F5: Federal Register 纯日期 (naive) 与 RFC822 (aware) 混合不得 TypeError。"""
    left = _event("a")
    left.headline = "Company files 8-K for spectrum transaction"
    left.source_url = "https://a.example/1"
    left.event_time = "2026-10-09"  # naive date
    right = _event("b")
    right.headline = "Company files 8-K for spectrum deal"
    right.source_url = "https://b.example/2"
    right.event_time = "Thu, 09 Oct 2026 12:30:00 GMT"  # RFC822 aware
    clusters = deduplicate_events([left, right])
    assert len(clusters) == 1  # 同日 12h 内, 走标题+时间窗正常合并 (非 URL 短路)


def test_relevance_ticker_not_fooled_by_hyphenated_name():
    """F6: "T-Mobile" 不得误命中 ticker T; 公司名仍正常命中 TMUS。"""
    event = _event()
    event.headline = "T-Mobile unveils new plan"
    context = RelevanceContext(
        symbol_names={"T": "AT&T Inc.", "TMUS": "T-Mobile"},
        sector_symbols={},
    )
    match = filter_relevant_events([event], context)
    assert len(match) == 1  # 命中来自 TMUS 公司名
    from app.event_stream.relevance import match_event_relevance

    relevance = match_event_relevance(event, context)
    assert relevance is not None
    assert "T" not in relevance.symbols
    assert "TMUS" in relevance.symbols


def test_dedupe_does_not_collapse_sec_form_titles():
    """2026-10-17 审计: 186 条不同公司的 8-K 曾因标题归一成 "8 k" 被聚成 1 条。"""
    events = []
    for index, company in enumerate(("COST", "V", "GS", "PEP", "TERRA")):
        event = _event(f"sec-{index}")
        event.source_id = "SEC_EDGAR_8K_ATOM"
        event.source_tier = "T0"
        event.source_url = f"https://sec.gov/{company}"
        event.headline = f"8-K - {company} (000000000{index}) (Filer)"
        event.event_time = f"2026-10-09T10:0{index}:00+00:00"
        events.append(event)

    clusters = deduplicate_events(events)
    assert len(clusters) == 5
    assert {cluster.primary.headline for cluster in clusters} == {
        event.headline for event in events
    }
