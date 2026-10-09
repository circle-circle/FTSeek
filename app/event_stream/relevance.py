from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

import yaml

from app.catalyst.event_classifier import SECTOR_TRIGGERS
from app.event_stream.models import CanonicalEvent


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


def match_event_relevance(
    event: CanonicalEvent,
    context: RelevanceContext,
) -> Optional[RelevanceMatch]:
    raw_text = " ".join(
        part
        for part in (event.headline, event.summary, str(event.facts))
        if part
    )
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
        if trigger in text:
            symbols = context.sector_symbols.get(sector_key, [])
            matched_symbols.update(symbols)
            if symbols:
                reasons.append(f"sector:{sector_key}")

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
