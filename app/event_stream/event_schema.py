# -*- coding: utf-8 -*-
"""统一事件元数据 (event_meta_v1, 2026-10-18).

目标: 给 EventStream 的每条 canonical event 一个稳定的结构化描述, 供 UE/DA
按"事件族/表单/实体/材料性"消费, 而不是只靠 headline + 关键词相关性.

边界:
  - 纯确定性函数, 不做 LLM 判断, 不发网络请求.
  - materiality 是**先验排序分**, 不是预测收益; 用于 clue 排序/审计, 不作交易门.
  - 实体解析先做保守版 (facts 里的 symbols/tickers + CIK 正则 + SEC 公司名),
    完整 CIK/ticker/FIGI 映射后续接 OpenFIGI/SEC company_tickers.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.event_stream.models import CanonicalEvent

logger = logging.getLogger(__name__)

EVENT_META_VERSION = "event_meta_v1"
_TICKER_MAP_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "cache" / "edgar_company_tickers.json"
)

# 标题 ticker 抽取只认"明确指向股票主体"的上下文, 避免 CEO/FCC/AI 这类全大写词
# 被当成 ticker (10/20 ASTS 案: 标题带 ASTS 但 entities.symbols 为空, 证据匹配只剩
# T/TMUS/VZ 等同业, 主体缺失 → citation_state 只能落 transmission_assumed).
_TICKER_STOPWORDS = frozenset({
    "A", "I", "AI", "ALL", "ON", "NOW", "KEY", "LOW", "NEW", "ONE", "TWO", "SIX",
    "TEN", "BIG", "HOT", "WIN", "TOP", "THE", "AND", "FOR", "WITH", "FROM", "THIS",
    "THAT", "ARE", "WAS", "HAS", "NOT", "BUT", "YOU", "CAN", "MAY", "OUT", "UP",
    "DOWN", "BUY", "SELL", "HOLD", "CEO", "CFO", "CTO", "COO", "SEC", "FCC", "FTC",
    "FDA", "DOJ", "EPA", "US", "USA", "GDP", "CPI", "PPI", "NFP", "PMI", "ISM",
    "FED", "GOP", "DOW", "RUT", "FOMC", "IPO", "ETF", "EV",
    "EPS", "PE", "FY", "Q1", "Q2", "Q3", "Q4", "ESG", "IRS", "FBI", "CDC", "WHO",
    "NATO", "EU", "UK", "UN", "IMF", "OPEC", "NYSE", "NASDAQ", "AMEX", "SPAC",
    "REIT", "ADR", "VIX", "SPX", "NDX", "DJIA", "YOY", "QOQ", "MOM", "TAM",
    "SAM", "SOM", "ROI", "ROE", "YTD", "ATH", "ATR", "RSI", "MACD", "EMA",
    "VWAP", "DTE", "IV", "HV", "OI", "PT", "TP", "SL", "DD", "PM", "AM", "ET",
    "UTC", "GMT",
})
_TICKER_CONTEXT_RE = re.compile(
    r"\b([A-Z][A-Z0-9.\-]{1,4})\b(?=\s+(?:Stock|Stocks|Shares|Earnings|Results|"
    r"Guidance|Jumps|Slips|Surges|Falls|Rises|Plunges|Soars|Sinks|Slides|Tumbles|"
    r"Rallies|Gains|Drops|Climbs|Spikes|Crashes|Upgrade|Downgrade|Buy|Sell)\b)"
)
_LEADING_TICKER_RE = re.compile(r"^\s*([A-Z][A-Z0-9.\-]{1,4})(?=\s|[\u4e00-\u9fff])")
_PAREN_TICKER_RE = re.compile(r"\(([A-Z][A-Z0-9.\-]{0,5})\)")
_CASHTAG_RE = re.compile(r"\$([A-Za-z][A-Za-z0-9.\-]{0,5})\b")
_EXCHANGE_TICKER_RE = re.compile(
    r"\((?:NASDAQ|NYSE(?:\s+AMERICAN)?|AMEX|OTC(?:QB|QX)?)\s*:\s*"
    r"([A-Z][A-Z0-9.\-]{0,5})\)",
    re.IGNORECASE,
)


@lru_cache(maxsize=1)
def known_tickers() -> frozenset[str]:
    """SEC ticker→CIK 缓存作为已知 ticker 集; 缺失时退化为空集 (抽取器不生效)."""
    try:
        raw = json.loads(_TICKER_MAP_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return frozenset()
    except Exception as exc:
        logger.warning("[event_meta] ticker 映射加载失败 (标题抽取降级): %s", exc,
                       exc_info=True)
        return frozenset()
    if not isinstance(raw, dict):
        return frozenset()
    return frozenset(
        str(ticker).upper().removeprefix("US.")
        for ticker in raw
        if str(ticker).strip()
    )


def extract_headline_tickers(headline: str) -> List[str]:
    """从标题抽取明确指向股票主体的 ticker (保守规则, 宁可漏抽不误抽).

    只认四类信号: $TICKER / 交易所前缀 / (TICKER) / TICKER+股票语境词或行首。
    结果必须存在于 SEC ticker 映射, 且不在 _TICKER_STOPWORDS。
    """
    text = str(headline or "")
    if not text:
        return []
    known = known_tickers()
    if not known:
        return []
    candidates = set()
    for pattern in (_CASHTAG_RE, _EXCHANGE_TICKER_RE, _PAREN_TICKER_RE):
        for match in pattern.finditer(text):
            candidates.add(match.group(1).upper())
    for pattern in (_TICKER_CONTEXT_RE, _LEADING_TICKER_RE):
        for match in pattern.finditer(text):
            candidates.add(match.group(1).upper())
    return sorted(
        ticker for ticker in candidates
        if ticker in known and ticker not in _TICKER_STOPWORDS
    )

_FORM_ALIASES = {
    "8-K": "8-K", "8-K/A": "8-K/A",
    "10-Q": "10-Q", "10-K": "10-K", "6-K": "6-K",
    "FORM 4": "4", "FORM 3": "3", "FORM 5": "5", "4": "4", "3": "3", "5": "5",
    "SC 13D": "13D", "SC 13G": "13G", "13D": "13D", "13G": "13G",
    "SC 13D/A": "13D/A", "SC 13G/A": "13G/A",
    "S-1": "S-1", "S-3": "S-3",
}


def _norm_form(value: Any) -> str:
    raw = str(value or "").strip().upper()
    raw = re.sub(r"\s+", " ", raw)
    return _FORM_ALIASES.get(raw, raw)


def _form_from_headline(headline: str) -> str:
    match = re.match(r"^\s*(8-K/A|8-K|10-Q|10-K|6-K|FORM\s*[345]|SC\s+13[GD]|S-[13])\b",
                     headline or "", re.IGNORECASE)
    if not match:
        return ""
    return _norm_form(re.sub(r"\s+", " ", match.group(1).upper()))


def _event_family(event_type: str, form: str) -> str:
    et = str(event_type or "").upper()
    if form.startswith(("13D", "13G")):
        return "activist"
    if form in ("3", "4", "5"):
        return "insider"
    if form in ("10-Q", "10-K"):
        return "financial_report"
    if form.startswith("8-K") or et == "SEC_8K_FILING":
        return "filing_8k"
    if et == "FEDERAL_REGISTER_NOTICE":
        return "regulatory"
    if et in ("NEWS_DISCOVERY", "NEWS_WIRE"):
        return "news"
    return "other"


def _as_symbols(value: Any) -> List[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, Iterable) and not isinstance(value, (dict, bytes)):
        values = list(value)
    else:
        values = []
    out = []
    for item in values:
        symbol = str(item or "").strip().upper().removeprefix("US.")
        if symbol and re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", symbol):
            out.append(symbol)
    return sorted(set(out))


def _entities_from_parts(
    *,
    headline: str,
    summary: str,
    source_id: str,
    symbols: Iterable[Any] = (),
    company: Optional[str] = None,
    cik: Optional[str] = None,
) -> Dict[str, Any]:
    merged = list(_as_symbols(symbols))
    merged.extend(extract_headline_tickers(headline))
    text = f"{headline or ''} {summary or ''}"
    if not cik:
        cik_match = re.search(r"(?:CIK\s*)?\(?(\d{10})\)?", text)
        cik = cik_match.group(1) if cik_match else None
    if company is None and str(source_id or "").upper().startswith("SEC_") and " - " in (headline or ""):
        after = headline.split(" - ", 1)[1]
        company = re.sub(r"\s*\(\d{10}\).*$", "", after).strip() or None
    return {
        "symbols": sorted(set(merged)),
        "cik": cik,
        "company": company,
    }


def _materiality(tier: str, form: str, family: str, event_type: str) -> float:
    base = {"T0": 0.65, "T1": 0.45, "T2": 0.25}.get(str(tier or "").upper(), 0.2)
    form_bonus = {
        "8-K": 0.15, "8-K/A": 0.10,
        "13D": 0.15, "13G": 0.12, "13D/A": 0.12, "13G/A": 0.10,
        "4": 0.05, "3": 0.03, "5": 0.03,
        "10-Q": 0.10, "10-K": 0.10, "6-K": 0.05,
        "S-1": 0.08, "S-3": 0.06,
    }.get(form, 0.0)
    family_bonus = {
        "filing_8k": 0.05, "activist": 0.03, "insider": 0.02,
        "financial_report": 0.03, "regulatory": 0.03, "news": 0.0,
    }.get(family, 0.0)
    if str(event_type or "").upper() == "SEC_8K_FILING":
        family_bonus += 0.02
    return round(min(1.0, max(0.0, base + form_bonus + family_bonus)), 4)


def build_event_meta_from_parts(
    *,
    source_id: str,
    source_tier: str,
    event_type: str,
    form: str = "",
    headline: str = "",
    summary: str = "",
    symbols: Iterable[Any] = (),
    company: Optional[str] = None,
    cik: Optional[str] = None,
    event_time: Optional[str] = None,
) -> Dict[str, Any]:
    """构造 event_meta_v1 (非 canonical event 来源也可用, 如 UE 的 8-K/新闻事实).

    与 build_event_meta 同一套 family/form/materiality/entities 规则, 避免
    EventStream 与直连 SEC/新闻通道出现两套口径。
    """
    normalized_form = _norm_form(form or _form_from_headline(headline))
    family = _event_family(event_type, normalized_form)
    entities = _entities_from_parts(
        headline=headline,
        summary=summary,
        source_id=source_id,
        symbols=symbols,
        company=company,
        cik=cik,
    )
    return {
        "schema": EVENT_META_VERSION,
        "source_id": source_id,
        "source_tier": source_tier,
        "event_type": event_type,
        "event_family": family,
        "form": normalized_form or None,
        "entities": entities,
        "materiality": _materiality(source_tier, normalized_form, family, event_type),
        "novelty": "unknown",       # 跨源 novelty 待统一事件去重后补
        "corroboration": 1,         # 单源计数, 待跨源 corroboration 后补
        "lifecycle": "new",
        "event_time": event_time,
    }


def build_event_meta(event: CanonicalEvent) -> Dict[str, Any]:
    """构造 event_meta_v1; 纯函数, 不修改 event."""
    facts = event.facts if isinstance(event.facts, dict) else {}
    return build_event_meta_from_parts(
        source_id=event.source_id,
        source_tier=event.source_tier,
        event_type=event.event_type,
        form=str(facts.get("form") or ""),
        headline=event.headline or "",
        summary=event.summary or "",
        symbols=(
            _as_symbols(facts.get("symbols"))
            + _as_symbols(facts.get("tickers"))
            + _as_symbols(facts.get("symbol"))
            + _as_symbols(facts.get("ticker"))
        ),
        event_time=event.event_time,
    )
