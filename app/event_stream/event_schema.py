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

import re
from collections.abc import Iterable
from typing import Any, Dict, List, Optional

from app.event_stream.models import CanonicalEvent

EVENT_META_VERSION = "event_meta_v1"

_FORM_ALIASES = {
    "8-K": "8-K", "8-K/A": "8-K/A",
    "10-Q": "10-Q", "10-K": "10-K", "6-K": "6-K",
    "FORM 4": "4", "FORM 3": "3", "FORM 5": "5", "4": "4", "3": "3", "5": "5",
    "SC 13D": "13D", "SC 13G": "13G", "13D": "13D", "13G": "13G",
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
    if form in ("13D", "13G"):
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


def _entities(event: CanonicalEvent, form: str) -> Dict[str, Any]:
    facts = event.facts if isinstance(event.facts, dict) else {}
    symbols = []
    for key in ("symbols", "tickers", "symbol", "ticker"):
        symbols.extend(_as_symbols(facts.get(key)))
    text = f"{event.headline or ''} {event.summary or ''}"
    cik_match = re.search(r"(?:CIK\s*)?\(?(\d{10})\)?", text)
    cik = cik_match.group(1) if cik_match else None
    company = None
    if str(event.source_id or "").upper().startswith("SEC_") and " - " in (event.headline or ""):
        after = event.headline.split(" - ", 1)[1]
        company = re.sub(r"\s*\(\d{10}\).*$", "", after).strip() or None
    return {
        "symbols": sorted(set(symbols)),
        "cik": cik,
        "company": company,
    }


def _materiality(tier: str, form: str, family: str, event_type: str) -> float:
    base = {"T0": 0.65, "T1": 0.45, "T2": 0.25}.get(str(tier or "").upper(), 0.2)
    form_bonus = {
        "8-K": 0.15, "8-K/A": 0.10,
        "13D": 0.15, "13G": 0.12,
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


def build_event_meta(event: CanonicalEvent) -> Dict[str, Any]:
    """构造 event_meta_v1; 纯函数, 不修改 event."""
    facts = event.facts if isinstance(event.facts, dict) else {}
    form = _norm_form(facts.get("form") or _form_from_headline(event.headline or ""))
    family = _event_family(event.event_type, form)
    entities = _entities(event, form)
    return {
        "schema": EVENT_META_VERSION,
        "source_id": event.source_id,
        "source_tier": event.source_tier,
        "event_type": event.event_type,
        "event_family": family,
        "form": form or None,
        "entities": entities,
        "materiality": _materiality(event.source_tier, form, family, event.event_type),
        "novelty": "unknown",       # 跨源 novelty 待统一事件去重后补
        "corroboration": 1,         # 单源计数, 待跨源 corroboration 后补
        "lifecycle": "new",
        "event_time": event.event_time,
    }
