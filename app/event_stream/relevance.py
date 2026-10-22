from __future__ import annotations

import html
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

import yaml

from app.catalyst.event_classifier import SECTOR_TRIGGERS
from app.event_stream.event_schema import extract_headline_tickers
from app.event_stream.models import CanonicalEvent

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_SEC_FORM_PREFIX_RE = re.compile(
    r"^\s*(?:8-K/A|8-K|10-Q|10-K|6-K|SC\s+13[DG](?:/A)?|FORM\s*[345])\b",
    re.IGNORECASE,
)
_SEC_STATE_SUFFIX_RE = re.compile(r"\s*/\s*[A-Z]{2}\b")


@dataclass
class RelevanceContext:
    symbol_names: dict[str, str]
    sector_symbols: dict[str, List[str]]


@dataclass
class RelevanceMatch:
    symbols: List[str]
    reasons: List[str]


def load_relevance_context(config_path: Optional[Path] = None) -> RelevanceContext:
    path = config_path or Path(__file__).resolve().parents[2] / "config" / "unified_config.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    symbol_names: dict[str, str] = {}
    for row in (data.get("watchlist", {}).get("us_stocks", []) or []):
        if not row.get("enabled", False):
            continue
        code = str(row.get("code", "")).removeprefix("US.")
        name = str(row.get("name", "")).strip()
        if code:
            symbol_names[code] = name

    sector_symbols = {
        str(key): [str(symbol).removeprefix("US.") for symbol in symbols]
        for key, symbols in (data.get("catalyst", {}).get("sector_mapping", {}) or {}).items()
    }
    return RelevanceContext(symbol_names=symbol_names, sector_symbols=sector_symbols)


def _plain_text(text: str) -> str:
    """去掉 Google RSS description 里的 HTML 标签/实体。"""
    return html.unescape(_HTML_TAG_RE.sub(" ", text or ""))


def _strip_source_marker(text: str, source: object, *, strip_tail: bool = False) -> str:
    """去掉文本末尾的 " - 发布方" 或 HTML 清洗后残留的发布方尾注。

    2026-10-17 审计: `Spectrum News 13` 这类发布方后缀会命中 telecom 触发词,
    把与电信无关的新闻误扩到 T/TMUS/VZ。只剥 facts.source 精确匹配的尾部标记,
    不做通用 " - " 截断, 避免误伤 "T-Mobile - AT&T" 这类标题。
    """
    source_name = str(source or "").strip()
    if not source_name:
        return text
    suffix = f" - {source_name}"
    if text.casefold().endswith(suffix.casefold()):
        return text[: -len(suffix)].rstrip()
    if strip_tail:
        # Google description 清洗 HTML 后常以 "标题&nbsp;&nbsp;发布方" 结尾, 没有 " - ".
        tail_marker = re.compile(rf"(?:[\s\-–—|:]+){re.escape(source_name)}\s*$", re.I)
        return tail_marker.sub("", text).rstrip()
    return text


def _strip_sec_state_suffix(text: str) -> str:
    """SEC filing 标题的公司名常带 " / MA" 等注册州后缀 — 裸 ticker 匹配会误中 MA。

    2026-10-21 实测: "8-K - VERTEX PHARMACEUTICALS INC / MA (0000155523)" 被
    匹配成 Mastercard (MA), 导致证据实体错配。只对 SEC form 标题剥该后缀。
    """
    if _SEC_FORM_PREFIX_RE.match(text or ""):
        return _SEC_STATE_SUFFIX_RE.sub("", text)
    return text


def _trigger_in_text(trigger: str, text: str) -> bool:
    """英文触发词按词边界匹配; 中文无词边界, 保持子串匹配。"""
    trigger = trigger.casefold()
    if re.fullmatch(r"[a-z0-9][a-z0-9 \-]*", trigger):
        return re.search(
            rf"(?<![a-z0-9]){re.escape(trigger)}(?![a-z0-9])", text
        ) is not None
    return trigger in text


def match_event_relevance(
    event: CanonicalEvent,
    context: RelevanceContext,
) -> Optional[RelevanceMatch]:
    source = event.facts.get("source")
    headline = _strip_sec_state_suffix(
        _strip_source_marker(_plain_text(event.headline), source))
    # 2026-10-17 审计: facts 里的 query/term 是检索条件, 不是事件文本 —
    #   Google 查询 "spectrum OR ..." 会让每条结果都命中 telecom。只匹配标题和摘要。
    summary_raw = event.summary or ""
    summary = _strip_source_marker(
        _plain_text(summary_raw), source, strip_tail=bool(_HTML_TAG_RE.search(summary_raw))
    )
    raw_text = " ".join(part for part in (headline, summary) if part)
    text = raw_text.casefold()
    matched_symbols = set()
    reasons = []

    for symbol, name in context.symbol_names.items():
        # 2026-10-09 F6 修复: lookahead 加 "-" — 否则 "T-Mobile" 标题里的独立 T
        #   会误命中 ticker T (AT&T); 公司名分支不受影响 (T-Mobile 走 name 匹配 TMUS)。
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(symbol)}(?![A-Za-z0-9-])", raw_text):
            matched_symbols.add(symbol)
            reasons.append(f"watchlist:{symbol}")
        if name and re.search(rf"(?<![a-z0-9]){re.escape(name.casefold())}(?![a-z0-9])", text):
            matched_symbols.add(symbol)
            reasons.append(f"watchlist-name:{symbol}")

    for trigger, sector_key in SECTOR_TRIGGERS.items():
        if _trigger_in_text(trigger, text):
            symbols = context.sector_symbols.get(sector_key, [])
            matched_symbols.update(symbols)
            if symbols:
                reasons.append(f"sector:{sector_key}")

    # 2026-10-21: 标题里明确点名的 ticker 必须进入 symbols — 否则像
    #   "ASTS Stock Slips..." 这类新闻只剩 telecom 同业 T/TMUS/VZ, 主体 ASTS
    #   缺席, 证据只能落 transmission_assumed (10/20 实测)。
    for ticker in extract_headline_tickers(headline):
        if ticker not in matched_symbols:
            matched_symbols.add(ticker)
            reasons.append(f"headline-ticker:{ticker}")

    if not matched_symbols:
        return None
    return RelevanceMatch(
        symbols=sorted(matched_symbols),
        reasons=sorted(set(reasons)),
    )


def filter_relevant_events(
    events: Iterable[CanonicalEvent],
    context: RelevanceContext,
) -> List[CanonicalEvent]:
    return [event for event in events if match_event_relevance(event, context) is not None]
