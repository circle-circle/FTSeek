"""
SmartRelevanceEngine 单测 (N7-N11, 2026-05-14)

覆盖:
- TickerDictionary 静态词集 + 反查
- 上下文消歧 ("苹果" 歧义 vs Apple+iPhone 确证)
- 板块传导 (TSMC drop → NVDA/AMD 关联)
- 综合评分边界
"""
import pytest


# ─── L0 TickerDictionary ────────────────────────────────
def test_ticker_dict_static_entities_loaded():
    from app.news_v2.quality.ticker_dictionary import get_ticker_dictionary
    td = get_ticker_dictionary()
    # 知名标的应在静态词集
    assert td.get("NVDA") is not None
    assert td.get("AAPL") is not None
    assert td.get("TSLA") is not None


def test_ticker_dict_lookup_by_chinese_name():
    from app.news_v2.quality.ticker_dictionary import get_ticker_dictionary
    td = get_ticker_dictionary()
    assert td.lookup_by_name("英伟达") == "NVDA"
    assert td.lookup_by_name("特斯拉") == "TSLA"
    assert td.lookup_by_name("台积电") == "TSM"


def test_ticker_dict_lookup_by_signal_word():
    from app.news_v2.quality.ticker_dictionary import get_ticker_dictionary
    td = get_ticker_dictionary()
    # iPhone → AAPL
    nvda_candidates = td.lookup_by_signal("iPhone")
    assert "AAPL" in nvda_candidates
    # H100 → NVDA
    h100 = td.lookup_by_signal("H100")
    assert "NVDA" in h100
    # 黄仁勋 → NVDA
    huang = td.lookup_by_signal("黄仁勋")
    assert "NVDA" in huang


def test_ticker_dict_add_entity_runtime():
    from app.news_v2.quality.ticker_dictionary import TickerDictionary, TickerEntity
    td = TickerDictionary()
    td.add_entity(TickerEntity(
        ticker="ABC",
        names=["ABC Corp", "ABC公司"],
        products=["product_x"],
    ))
    assert td.get("ABC") is not None
    assert td.lookup_by_name("ABC Corp") == "ABC"
    assert "ABC" in td.lookup_by_signal("product_x")


# ─── L1+L2 SmartRelevanceEngine ────────────────────────────────
def test_smart_relevance_clear_apple_news():
    """'Apple 发布 iPhone 16' 应明确识别为 AAPL"""
    from app.news_v2.quality.smart_relevance import get_smart_relevance
    engine = get_smart_relevance()
    rel = engine.evaluate(
        title="Apple 发布 iPhone 16, 库克: AI 是核心",
        content="Tim Cook 在发布会展示 Apple Intelligence 功能"
    )
    assert rel.is_relevant
    assert "AAPL" in rel.primary_tickers
    # 应该有信号词 (iPhone / Cook)
    aapl_match = rel.matches.get("AAPL")
    assert aapl_match is not None
    assert aapl_match.signal_words_hit
    assert aapl_match.is_disambiguated


def test_smart_relevance_ambiguous_apple_no_signal():
    """'苹果汁好喝' 不应识别为 AAPL"""
    from app.news_v2.quality.smart_relevance import get_smart_relevance
    engine = get_smart_relevance()
    rel = engine.evaluate(
        title="苹果汁价格上涨",
        content="今年苹果减产, 苹果汁批发价上升"
    )
    # AAPL 命中但被歧义降级 → 分数低
    if "AAPL" in rel.matches:
        aapl_match = rel.matches["AAPL"]
        # 歧义降级应使其不进入 primary_tickers
        assert "AAPL" not in rel.primary_tickers


def test_smart_relevance_chinese_news_with_signals():
    """'英伟达 H100 短缺, 黄仁勋警告' 应识别为 NVDA"""
    from app.news_v2.quality.smart_relevance import get_smart_relevance
    engine = get_smart_relevance()
    rel = engine.evaluate(
        title="英伟达 H100 供应紧张",
        content="黄仁勋表示 Blackwell 生产爬坡顺利, CUDA 生态领先"
    )
    assert rel.is_relevant
    assert "NVDA" in rel.primary_tickers
    nvda_match = rel.matches["NVDA"]
    # 多个信号词命中
    assert len(nvda_match.signal_words_hit) >= 2


def test_smart_relevance_macro_event():
    """美联储加息消息无 ticker 仍相关"""
    from app.news_v2.quality.smart_relevance import get_smart_relevance
    engine = get_smart_relevance()
    rel = engine.evaluate(
        title="美联储维持利率不变",
        content="鲍威尔表示通胀压力仍在, FOMC 决议公布"
    )
    assert rel.is_relevant
    assert rel.macro_hit


def test_smart_relevance_geo_transmission_china_tension():
    """'中美关系紧张' 应触发 China ADR / 半导体相关"""
    from app.news_v2.quality.smart_relevance import get_smart_relevance
    engine = get_smart_relevance()
    rel = engine.evaluate(
        title="China tension escalates over taiwan issue",
        content="US sanctions on chinese tech companies"
    )
    # GEO_TRANSMISSION 中 china_tension → BABA, PDD, JD, NIO, TSM, BIDU
    related = set(rel.related_tickers)
    assert any(tk in related for tk in ("BABA", "PDD", "TSM"))


def test_smart_relevance_irrelevant_news():
    """A 股茅台新闻应判不相关"""
    from app.news_v2.quality.smart_relevance import get_smart_relevance
    engine = get_smart_relevance()
    rel = engine.evaluate(
        title="贵州茅台股价创新高",
        content="白酒板块整体走强"
    )
    assert not rel.is_relevant


def test_smart_relevance_watchlist_bonus():
    """watchlist 内的 ticker 应得 bonus"""
    from app.news_v2.quality.smart_relevance import SmartRelevanceEngine
    engine = SmartRelevanceEngine(watchlist_codes={"US.NVDA"})
    rel = engine.evaluate(title="Nvidia announces new H100 GPU")
    assert "NVDA" in rel.primary_tickers
    # watchlist 加成使分数 > 普通命中
    assert rel.overall_score > 0.6


# ─── 边界 ────────────────────────────────
def test_smart_relevance_empty_input():
    from app.news_v2.quality.smart_relevance import get_smart_relevance
    engine = get_smart_relevance()
    rel = engine.evaluate("", "")
    assert not rel.is_relevant
    assert rel.overall_score == 0.0


def test_smart_relevance_unicode_safe():
    """日文/韩文等不应崩"""
    from app.news_v2.quality.smart_relevance import get_smart_relevance
    engine = get_smart_relevance()
    rel = engine.evaluate("ホンダの第4四半期決算が発表", "日本の自動車")
    # 不要崩, is_relevant 取决于 ticker 命中, 此例 0 命中应不相关
    assert rel.overall_score >= 0


# ─── L3 Embedding (可选, lazy 加载) ──────────────────────
def test_embedding_matcher_lazy_load_no_dep():
    """sentence-transformers 未装 → is_available 返回 False, 不崩"""
    from app.news_v2.quality.embedding_matcher import EmbeddingMatcher
    em = EmbeddingMatcher()
    # 未装时 _ensure_loaded → False
    available = em.is_available()
    # 不崩即可 (不强制 True/False, 取决于环境)
    assert available in (True, False)


def test_embedding_matcher_match_when_unavailable_returns_empty():
    """不可用时 match_news_to_tickers 返回 []"""
    from app.news_v2.quality.embedding_matcher import EmbeddingMatcher
    em = EmbeddingMatcher()
    # 强制 disabled
    em._available = False
    result = em.match_news_to_tickers("Some news")
    assert result == []


# ─── 接入 3 个新闻源后的整合测试 ──────────────────────
def test_cailianpress_uses_smart_relevance():
    """CailianPressSource._parse 经过 SmartRelevance 过滤"""
    from app.news_v2.sources.cailianpress_source import CailianPressSource
    import time
    s = CailianPressSource()
    telegrams = [
        # 应该相关 (NVDA + H100 + 黄仁勋)
        {"id": "t1", "title": "英伟达 H100 短缺", "content": "黄仁勋表示供货紧张", "ctime": time.time()},
        # 应该不相关 (A 股, 无美股 ticker)
        {"id": "t2", "title": "贵州茅台创新高", "content": "白酒板块走强", "ctime": time.time()},
    ]
    items = s._parse(telegrams)
    # 至少 t1 应该入库
    assert any("英伟达" in i.title for i in items)
    # t2 应该被过滤掉
    assert not any("茅台" in i.title for i in items)


def test_smart_relevance_module_importable():
    """所有新模块可 import"""
    import importlib
    for m in [
        "app.news_v2.quality.ticker_dictionary",
        "app.news_v2.quality.smart_relevance",
        "app.news_v2.quality.embedding_matcher",
    ]:
        importlib.import_module(m)
