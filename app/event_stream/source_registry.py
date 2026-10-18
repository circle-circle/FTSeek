from __future__ import annotations

from typing import Callable, Dict

from app.event_stream.models import SourceResult
from app.event_stream.sources.federal_register import fetch_federal_register
from app.event_stream.sources.finnhub_news import fetch_finnhub_news
from app.event_stream.sources.google_news_rss import fetch_google_news_rss
from app.event_stream.sources.sec_edgar_atom import fetch_sec_edgar_atom
from app.event_stream.sources.sec_provider import fetch_sec_filings_provider


def build_default_registry(provider_enabled: bool = False) -> Dict[str, Callable[[], SourceResult]]:
    """构造默认源注册表.

    provider_enabled=True 时用 SEC_FILINGS_PROVIDER 替换 raw SEC_EDGAR_8K_ATOM,
    并补 SC 13D/G; 两者二选一, 避免同一批 8-K 被采两次.
    """
    if provider_enabled:
        return {
            "SEC_FILINGS_PROVIDER": fetch_sec_filings_provider,
            "FEDERAL_REGISTER_API": fetch_federal_register,
            "GOOGLE_NEWS_RSS": fetch_google_news_rss,
            "FINNHUB_GENERAL_NEWS": fetch_finnhub_news,
        }
    return {
        "SEC_EDGAR_8K_ATOM": fetch_sec_edgar_atom,
        "FEDERAL_REGISTER_API": fetch_federal_register,
        "GOOGLE_NEWS_RSS": fetch_google_news_rss,
        "FINNHUB_GENERAL_NEWS": fetch_finnhub_news,
    }


def default_source_registry() -> Dict[str, Callable[[], SourceResult]]:
    try:
        from app.core.config_container import get_config
        enabled = bool(get_config().get_dotted(
            "event_stream.sec_provider_source_enabled", False))
    except Exception:
        enabled = False
    return build_default_registry(enabled)
