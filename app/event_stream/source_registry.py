from __future__ import annotations

from typing import Callable, Dict

from app.event_stream.models import SourceResult
from app.event_stream.sources.federal_register import fetch_federal_register
from app.event_stream.sources.finnhub_news import fetch_finnhub_news
from app.event_stream.sources.google_news_rss import fetch_google_news_rss
from app.event_stream.sources.sec_edgar_atom import fetch_sec_edgar_atom


def default_source_registry() -> Dict[str, Callable[[], SourceResult]]:
    return {
        "SEC_EDGAR_8K_ATOM": fetch_sec_edgar_atom,
        "FEDERAL_REGISTER_API": fetch_federal_register,
        "GOOGLE_NEWS_RSS": fetch_google_news_rss,
        "FINNHUB_GENERAL_NEWS": fetch_finnhub_news,
    }
