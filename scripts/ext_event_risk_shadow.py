#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""EXT 在途事件再评估影子 (RFC-205 / DEB-20261106 §65.4 P2) · 只读离线

用途
----
对一笔**已平仓**的 EXT 交易，用 `event_stream.db::canonical_events` 的
`first_seen_at` 时间线做一次确定性回看：

  - 只看 `entry_time < first_seen_at <= as_of`（as_of 默认 = exit_time）的事件；
  - 事件必须与标的**直接相关**（symbol / entity / company / headline / summary / facts）；
  - 先按 URL 与标题去重，保留最早可见的那条；
  - 输出三态：`NO_CHANGE` / `REVIEW` / `INVALIDATE`；
  - `INVALIDATE` 仅允许**官方/一手来源**且文本含**显式**反证短语；未验证传闻只能
    触发 `REVIEW`，永远不能触发 `INVALIDATE`；
  - 不计算任何“避免了多少亏损”的反事实 PnL：没有事件窗口 bars 时
    `counterfactual.status=unavailable`。

硬边界
------
  - 只读：两个 SQLite 均以 `file:...?mode=ro` 打开并 `PRAGMA query_only=ON`；
  - 零生产写入、零下单、零 cron、零 registry、零消费者、零自动平仓；
  - 产物写到 `data/shadow/ext_event_risk/`，CLI 拒绝覆盖已存在文件（`O_EXCL`）。

用法
----
    ./venv/bin/python scripts/ext_event_risk_shadow.py
    ./venv/bin/python scripts/ext_event_risk_shadow.py --as-of 2026-11-12T01:44:03Z
    ./venv/bin/python scripts/ext_event_risk_shadow.py --print-json
    ./venv/bin/python scripts/ext_event_risk_shadow.py --out /tmp/nvda-review.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TRADE_DB = ROOT / "data" / "ftseek_us.db"
DEFAULT_EVENT_DB = ROOT / "data" / "event_stream.db"
DEFAULT_OUT_DIR = ROOT / "data" / "shadow" / "ext_event_risk"
DEFAULT_TRADE_ID = 1864
DEFAULT_DECISION_ID = "EXT-2026-11-11-US.NVDA-afterhours"

SCHEMA = "ext_event_risk_shadow.v1"
MATERIALITY_THRESHOLD = 0.40

#: DEB-20261106 §65 冻结案例集的最小别名种子。其他标的可经 `--alias` 追加；
#: 不从 app.news_v2 导入，避免离线脚本拉起新闻管线。
SYMBOL_ALIASES: Dict[str, Tuple[str, ...]] = {
    "NVDA": ("Nvidia", "NVIDIA Corp"),
    "AMD": ("Advanced Micro Devices", "AMD"),
    "AVGO": ("Broadcom", "Broadcom Inc"),
    "TSM": ("TSMC", "Taiwan Semiconductor"),
    "MU": ("Micron", "Micron Technology"),
    "SNDK": ("SanDisk", "Sandisk"),
    "ARM": ("Arm Holdings", "Arm"),
    "NBIS": ("Nebius", "Nebius Group"),
}

EVENT_COLUMNS = (
    "event_id",
    "source_id",
    "source_tier",
    "source_url",
    "event_type",
    "event_time",
    "headline",
    "summary",
    "facts_json",
    "status",
    "confidence",
    "reasoning_version",
    "first_seen_at",
    "published_to_seen_ms",
    "event_meta_json",
)

# ---------------------------------------------------------------------------
# 时间 / 哈希 / JSON
# ---------------------------------------------------------------------------


def _parse_ts(value: Any, *, field: str = "timestamp") -> datetime:
    """ISO 时间 → aware UTC；无时区视为错误（本影子不接受隐式时区）。"""
    if value is None or str(value).strip() == "":
        raise ValueError(f"{field} is empty")
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError(f"{field} has no timezone offset: {value!r}")
    return parsed.astimezone(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _stable_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_obj(value: Any) -> str:
    return _sha256_text(_stable_json(value))


def _sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _parse_json_object(raw: Any) -> Tuple[Dict[str, Any], Optional[str]]:
    if raw is None or str(raw).strip() == "":
        return {}, None
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError) as exc:
        return {}, f"invalid_json: {type(exc).__name__}"
    if not isinstance(value, dict):
        return {}, "non_object_json"
    return value, None


# ---------------------------------------------------------------------------
# 只读 DB
# ---------------------------------------------------------------------------


def _open_ro(path: Path) -> sqlite3.Connection:
    resolved = Path(path).expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"database not found: {resolved}")
    uri = resolved.as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _load_trade(conn: sqlite3.Connection, trade_id: int) -> Dict[str, Any]:
    row = conn.execute("SELECT * FROM trade_records WHERE id=?", (trade_id,)).fetchone()
    if row is None:
        raise LookupError(f"trade_records.id={trade_id} not found")
    return dict(row)


def _load_events(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    sql = (
        "SELECT "
        + ", ".join(EVENT_COLUMNS)
        + " FROM canonical_events WHERE first_seen_at IS NOT NULL"
        + " ORDER BY first_seen_at, event_id"
    )
    events: List[Dict[str, Any]] = []
    for row in conn.execute(sql):
        event = _event_from_row(dict(row))
        events.append(event)
    return events


def _event_from_row(row: Dict[str, Any]) -> Dict[str, Any]:
    meta, meta_error = _parse_json_object(row.get("event_meta_json"))
    facts, facts_error = _parse_json_object(row.get("facts_json"))
    event = {
        "event_id": row.get("event_id"),
        "source_id": row.get("source_id"),
        "source_tier": row.get("source_tier"),
        "source_url": row.get("source_url"),
        "event_type": row.get("event_type"),
        "event_time": row.get("event_time"),
        "headline": row.get("headline") or "",
        "summary": row.get("summary") or "",
        "facts": facts,
        "facts_raw": row.get("facts_json") or "",
        "status": row.get("status"),
        "confidence": row.get("confidence"),
        "reasoning_version": row.get("reasoning_version"),
        "first_seen_at": row.get("first_seen_at"),
        "published_to_seen_ms": row.get("published_to_seen_ms"),
        "event_meta": meta,
        "parse_warnings": [warning for warning in (meta_error, facts_error) if warning],
    }
    try:
        event["_first_seen_dt"] = _parse_ts(row.get("first_seen_at"), field="first_seen_at")
    except ValueError as exc:
        event["_first_seen_dt"] = None
        event["parse_warnings"].append(str(exc))
    try:
        event["_event_time_dt"] = _parse_ts(row.get("event_time"), field="event_time")
    except ValueError:
        event["_event_time_dt"] = None
    return event


# ---------------------------------------------------------------------------
# 直接相关性 / 证据层级
# ---------------------------------------------------------------------------


def _ticker_from_symbol(symbol: str) -> str:
    text = str(symbol or "").strip().upper()
    if "." in text:
        text = text.rsplit(".", 1)[1]
    return text


def _alias_terms(ticker: str, extra_aliases: Sequence[str]) -> List[str]:
    terms = {ticker.upper()}
    for alias in SYMBOL_ALIASES.get(ticker, ()):  # noqa: E501
        terms.add(alias)
    for alias in extra_aliases:
        if str(alias).strip():
            terms.add(str(alias).strip())
    # 长的放前面，保证 matched_terms 可读；排序本身也保证确定性。
    return sorted(terms, key=lambda item: (-len(item), item.casefold()))


def _term_in_text(text: str, term: str) -> bool:
    if not text or not term:
        return False
    haystack = text.casefold()
    needle = term.casefold().strip()
    if not needle:
        return False
    # 短代码必须词边界匹配，防止 ARM 命中 alarm / MU 命中 museum。
    if re.fullmatch(r"[a-z0-9.\-]{1,6}", needle):
        return re.search(
            r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])",
            haystack,
        ) is not None
    return needle in haystack


def _matching_terms(text: str, terms: Sequence[str]) -> List[str]:
    return [term for term in terms if _term_in_text(text, term)]


def _facts_text(facts: Dict[str, Any], facts_raw: str) -> str:
    if facts:
        return _stable_json(facts)
    return facts_raw or ""


def _relevance(event: Dict[str, Any], ticker: str, terms: Sequence[str]) -> Dict[str, Any]:
    meta = event.get("event_meta") or {}
    entities = meta.get("entities") or {}
    symbol_values = {
        str(item).strip().upper()
        for item in (entities.get("symbols") or [])
        if str(item).strip()
    }
    company = str(entities.get("company") or "")
    cik = str(entities.get("cik") or "").strip()
    headline = event.get("headline") or ""
    summary = event.get("summary") or ""
    facts_text = _facts_text(event.get("facts") or {}, event.get("facts_raw") or "")

    reasons: List[str] = []
    matched: List[str] = []
    if ticker.upper() in symbol_values:
        reasons.append("entity_symbol")
        matched.append(ticker.upper())
    if company:
        company_matches = _matching_terms(company, terms)
        if company_matches:
            reasons.append("entity_company")
            matched.extend(company_matches)
    if cik and cik in (event.get("_known_ciks") or set()):
        reasons.append("entity_cik")
        matched.append(cik)
    for field_name, text in (("headline", headline), ("summary", summary), ("facts", facts_text)):
        hits = _matching_terms(text, terms)
        if hits:
            reasons.append(field_name)
            matched.extend(hits)
    # 去重且保持确定性。
    reasons = sorted(set(reasons))
    matched = sorted(set(matched), key=lambda item: (item.casefold(), item))
    return {"direct": bool(reasons), "reasons": reasons, "matched_terms": matched}


def _evidence_tier(event: Dict[str, Any]) -> str:
    source_tier = str(event.get("source_tier") or "").upper()
    source_id = str(event.get("source_id") or "").upper()
    event_type = str(event.get("event_type") or "").upper()
    if any(token in source_id for token in ("REDDIT", "STOCKTWITS", "TWITTER", "SOCIAL")):
        return "SOCIAL_UNVERIFIED"
    if (
        source_tier == "T0"
        or source_id.startswith("SEC_")
        or event_type.startswith("SEC_")
        or source_id.startswith("COMPANY_IR")
        or "_IR_" in source_id
    ):
        return "OFFICIAL_PRIMARY"
    if source_tier == "T1":
        return "MAJOR_MEDIA"
    if source_tier == "T2":
        return "DISCOVERY_RSS"
    return "OTHER"


def _materiality(event: Dict[str, Any]) -> Optional[float]:
    raw = (event.get("event_meta") or {}).get("materiality")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    return None


def _is_material(event: Dict[str, Any], threshold: float) -> bool:
    # 官方/一手来源天然按重大处理；其余必须带 event_meta.materiality 且过阈值。
    if event.get("evidence_tier") == "OFFICIAL_PRIMARY":
        return True
    value = _materiality(event)
    return value is not None and value >= threshold


# ---------------------------------------------------------------------------
# 去重
# ---------------------------------------------------------------------------


def _norm_url(url: str) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return text.casefold()
    host = parts.netloc.casefold()
    if host.startswith("www."):
        host = host[4:]
    path = re.sub(r"/+", "/", parts.path).rstrip("/")
    # 去掉 query/fragment：Google News 等聚合链接常带跟踪参数。
    return urlunsplit((parts.scheme.casefold(), host, path, "", ""))


def _norm_title(title: str) -> str:
    text = " ".join(str(title or "").casefold().split())
    if not text:
        return ""
    suffix = re.match(r"^(.*?)\s+[-|]\s+([^-|]{1,40})$", text)
    if suffix and len(suffix.group(2).split()) <= 4:
        text = suffix.group(1)
    text = re.sub(r"[^a-z0-9一-鿿]+", " ", text)
    return " ".join(text.split())


def _dedup(events: Sequence[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    kept: List[Dict[str, Any]] = []
    duplicates: List[Dict[str, Any]] = []
    seen_url: Dict[str, Dict[str, Any]] = {}
    seen_title: Dict[str, Dict[str, Any]] = {}
    for event in events:
        url_key = _norm_url(event.get("source_url") or "")
        title_key = _norm_title(event.get("headline") or "")
        reason = None
        original = None
        if url_key and url_key in seen_url:
            reason = "url"
            original = seen_url[url_key]
        elif title_key and title_key in seen_title:
            reason = "title"
            original = seen_title[title_key]
        if reason is not None:
            # 代表事件保留最早 first_seen；同组内若存在更可信/更重大的副本，
            # 由 evaluate() 把 material 属性提升到代表事件上，避免“早到的低阶
            # 副本把晚到的高阶副本去重掉”造成漏报。
            original.setdefault("_dedup_members", [original]).append(event)
            # 把副本自己的 URL/标题键也映射到同一个代表事件，保证传递性去重
            # （同 URL 不同标题 → 后续同标题副本仍归入该组）。
            if url_key:
                seen_url.setdefault(url_key, original)
            if title_key:
                seen_title.setdefault(title_key, original)
            duplicates.append(
                {
                    "duplicate_event_id": event.get("event_id"),
                    "kept_event_id": original.get("event_id"),
                    "reason": reason,
                    "duplicate_first_seen_at": event.get("first_seen_at"),
                    "kept_first_seen_at": original.get("first_seen_at"),
                    "url_key": url_key or None,
                    "title_key": title_key or None,
                }
            )
            continue
        event.setdefault("_dedup_members", [event])
        kept.append(event)
        if url_key:
            seen_url.setdefault(url_key, event)
        if title_key:
            seen_title.setdefault(title_key, event)
    return kept, duplicates


# ---------------------------------------------------------------------------
# 反证 / 严重度
# ---------------------------------------------------------------------------

_ROLE_RE = re.compile(
    r"\b(?:ceo|chief executive(?: officer)?|cfo|chief financial officer|"
    r"coo|chief operating officer|founder|president)\b",
    re.IGNORECASE,
)
_DEPARTURE_RE = re.compile(
    r"\b(?:resign(?:s|ed|ing|ation)?|depart(?:s|ed|ing|ure)?|"
    r"step(?:s|ped|ping)?\s+down|terminat(?:e|ed|ion)|fired|"
    r"oust(?:ed|er|ing)?|remov(?:e|ed|al)|retir(?:e|ed|ement))\b",
    re.IGNORECASE,
)

_LONG_CONTRADICTION_PATTERNS: Tuple[Tuple[str, re.Pattern[str]], ...] = (
    (
        "accounting_or_solvency",
        re.compile(
            r"\b(?:accounting fraud|restatement|material weakness|going concern|"
            r"bankruptcy|chapter 11|delisting)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "guidance_cut",
        re.compile(
            r"\b(?:cuts?|lowers?|withdraws?|suspends?)\b.{0,50}"
            r"\b(?:guidance|forecast|outlook)\b",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "regulatory_action",
        re.compile(
            r"\b(?:sec|doj|department of justice|commerce department|fbi)\b.{0,40}"
            r"\b(?:charges?|indicts?|bans?|blocks?|subpoena)\b",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "export_restriction",
        re.compile(
            r"\b(?:export (?:ban|restriction|control)s?|"
            r"banned from selling|sanctions?)\b",
            re.IGNORECASE,
        ),
    ),
)

_SHORT_CONTRADICTION_PATTERNS: Tuple[Tuple[str, re.Pattern[str]], ...] = (
    (
        "buyout_or_merger",
        re.compile(
            r"\b(?:acquires?|acquisition|buyout|take private|merger)\b.{0,60}"
            r"\b(?:premium|all-cash|completed|agreement)\b",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "guidance_raise",
        re.compile(
            r"\b(?:raises?|boosts?|lifts?)\b.{0,40}"
            r"\b(?:guidance|forecast|outlook)\b",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "earnings_beat",
        re.compile(
            r"\b(?:earnings beat|beats estimates|record revenue)\b",
            re.IGNORECASE,
        ),
    ),
)


def _excerpt(text: str, start: int, end: int, radius: int = 80) -> str:
    left = max(0, start - radius)
    right = min(len(text), end + radius)
    return " ".join(text[left:right].split())


def _leadership_crisis(text: str) -> Optional[Dict[str, Any]]:
    """CEO/CFO/founder 级离职/被免的**显式**短语。

    刻意不把 "director/officer" 当作 role：8-K Item 5.02 的通用样板
    "Departure of Directors or Certain Officers" 不能单独构成对某笔持仓的反证。
    """
    for role_match in _ROLE_RE.finditer(text):
        window_start = max(0, role_match.start() - 80)
        window_end = min(len(text), role_match.end() + 140)
        departure = _DEPARTURE_RE.search(text[window_start:window_end])
        if departure:
            start = window_start + departure.start()
            end = window_start + departure.end()
            return {
                "kind": "leadership_crisis",
                "role": role_match.group(0),
                "departure": departure.group(0),
                "excerpt": _excerpt(text, min(role_match.start(), start), max(role_match.end(), end)),
            }
    for departure in _DEPARTURE_RE.finditer(text):
        window_start = max(0, departure.start() - 100)
        window_end = min(len(text), departure.end() + 80)
        role = _ROLE_RE.search(text[window_start:window_end])
        if role:
            start = window_start + role.start()
            end = window_start + role.end()
            return {
                "kind": "leadership_crisis",
                "role": role.group(0),
                "departure": departure.group(0),
                "excerpt": _excerpt(text, min(departure.start(), start), max(departure.end(), end)),
            }
    return None


def _explicit_contradiction(
    event: Dict[str, Any],
    direction: str,
    terms: Sequence[str],
) -> Optional[Dict[str, Any]]:
    """官方/一手来源 + 文本显式反证，才返回证据；其余一律 None。"""
    if event.get("evidence_tier") != "OFFICIAL_PRIMARY":
        return None
    text = (event.get("headline") or "") + "\n" + (event.get("summary") or "")
    # 反证短语必须与标的出现在同一段文本里，防止“只有 metadata symbol”的误伤。
    if not _matching_terms(text, terms):
        return None

    leadership = _leadership_crisis(text)
    if leadership:
        return {
            "kind": leadership["kind"],
            "role": leadership["role"],
            "departure": leadership["departure"],
            "excerpt": leadership["excerpt"],
            "matched_text": _stable_json(
                {
                    "headline": event.get("headline"),
                    "summary": event.get("summary"),
                }
            ),
        }

    patterns = (
        _LONG_CONTRADICTION_PATTERNS
        if str(direction or "").upper() == "LONG"
        else _SHORT_CONTRADICTION_PATTERNS
    )
    for kind, pattern in patterns:
        match = pattern.search(text)
        if match:
            return {
                "kind": kind,
                "excerpt": _excerpt(text, match.start(), match.end()),
                "matched_text": match.group(0),
            }
    return None


def _severity(event: Dict[str, Any]) -> int:
    text = (event.get("headline") or "") + "\n" + (event.get("summary") or "")
    if _leadership_crisis(text):
        return 3
    if event.get("evidence_tier") == "OFFICIAL_PRIMARY":
        return 2
    if re.search(
        r"\b(?:probe|investigat(?:e|ion|ing)|subpoena|export ban|sanction|lawsuit|"
        r"fraud|resign(?:ed|ation)?)\b",
        text,
        re.IGNORECASE,
    ):
        return 2
    return 1


# ---------------------------------------------------------------------------
# 事件输出裁剪
# ---------------------------------------------------------------------------


def _public_event(event: Dict[str, Any]) -> Dict[str, Any]:
    first_seen_utc = (
        _iso_utc(event["_first_seen_dt"]) if event.get("_first_seen_dt") else None
    )
    event_time_utc = (
        _iso_utc(event["_event_time_dt"]) if event.get("_event_time_dt") else None
    )
    payload = {
        "event_id": event.get("event_id"),
        "source_id": event.get("source_id"),
        "source_tier": event.get("source_tier"),
        "source_url": event.get("source_url"),
        "event_type": event.get("event_type"),
        # 原始值逐字保留；UTC 归一值另列，便于审计。
        "event_time": event.get("event_time"),
        "event_time_utc": event_time_utc,
        "first_seen_at": event.get("first_seen_at"),
        "first_seen_at_utc": first_seen_utc,
        "published_to_seen_ms": event.get("published_to_seen_ms"),
        "headline": event.get("headline"),
        "summary": event.get("summary"),
        "status": event.get("status"),
        "confidence": event.get("confidence"),
        "reasoning_version": event.get("reasoning_version"),
        "evidence_tier": event.get("evidence_tier"),
        "materiality": _materiality(event),
        "material": event.get("material"),
        "severity": _severity(event),
        "relevance": event.get("relevance"),
        "dedup_group_event_ids": event.get("_dedup_group_event_ids") or [],
        "material_from_duplicate_event_ids": event.get("_material_from_duplicate_ids") or [],
        "parse_warnings": event.get("parse_warnings") or [],
    }
    return payload


def _sorted_by_first_seen(events: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        events,
        key=lambda event: (
            event.get("_first_seen_dt") or datetime.max.replace(tzinfo=timezone.utc),
            str(event.get("event_id") or ""),
        ),
    )


# ---------------------------------------------------------------------------
# 评估主流程
# ---------------------------------------------------------------------------


def evaluate(
    *,
    trade_db: Path,
    event_db: Path,
    trade_id: int,
    decision_id: Optional[str],
    as_of_override: Optional[str],
    extra_aliases: Sequence[str],
    materiality_threshold: float,
) -> Dict[str, Any]:
    trade_conn = _open_ro(trade_db)
    try:
        trade = _load_trade(trade_conn, trade_id)
    finally:
        trade_conn.close()

    actual_decision_id = trade.get("decision_id")
    if decision_id and actual_decision_id != decision_id:
        raise ValueError(
            f"decision_id mismatch: trade_records.id={trade_id} has "
            f"{actual_decision_id!r}, expected {decision_id!r}"
        )

    entry_dt = _parse_ts(trade.get("entry_time"), field="trade.entry_time")
    if as_of_override:
        as_of_dt = _parse_ts(as_of_override, field="--as-of")
        as_of_source = "cli_override"
    else:
        as_of_dt = _parse_ts(trade.get("exit_time"), field="trade.exit_time")
        as_of_source = "exit_time"
    if as_of_dt <= entry_dt:
        raise ValueError("as_of must be strictly after entry_time")

    ticker = _ticker_from_symbol(str(trade.get("symbol") or ""))
    if not ticker:
        raise ValueError("trade.symbol is empty or has no ticker suffix")
    terms = _alias_terms(ticker, extra_aliases)

    event_conn = _open_ro(event_db)
    try:
        all_events = _load_events(event_conn)
    finally:
        event_conn.close()

    included: List[Dict[str, Any]] = []
    excluded_before_or_at_entry = 0
    excluded_after_as_of = 0
    excluded_unparseable = 0
    for event in all_events:
        first_seen = event.get("_first_seen_dt")
        if first_seen is None:
            excluded_unparseable += 1
            continue
        if first_seen <= entry_dt:
            excluded_before_or_at_entry += 1
            continue
        if first_seen > as_of_dt:
            excluded_after_as_of += 1
            continue
        included.append(event)

    # CIK 映射：若 event_meta.symbols 含本标的，则把同一 CIK 的其他事件也算直接相关。
    known_ciks = set()
    for event in all_events:
        meta = event.get("event_meta") or {}
        entities = meta.get("entities") or {}
        symbols = {str(item).strip().upper() for item in (entities.get("symbols") or [])}
        cik = str(entities.get("cik") or "").strip()
        if cik and ticker.upper() in symbols:
            known_ciks.add(cik)

    for event in all_events:
        event["_known_ciks"] = known_ciks
        event["evidence_tier"] = _evidence_tier(event)
        event["relevance"] = _relevance(event, ticker, terms)
        event["material"] = bool(
            event["relevance"]["direct"]
            and _is_material(event, materiality_threshold)
        )

    direct_before_dedup = [
        event for event in included if event["relevance"]["direct"]
    ]
    direct_events, duplicates = _dedup(_sorted_by_first_seen(direct_before_dedup))

    # 同一 URL/标题组内只要有一条 material，代表事件（最早可见）也按 material
    # 处理；否则“早到的 T2 低重大副本”会把晚到的 T1 重大副本去重掉，造成漏报。
    for event in direct_events:
        members = event.get("_dedup_members") or [event]
        if len(members) > 1:
            event["_dedup_group_event_ids"] = [m.get("event_id") for m in members]
            material_members = [m for m in members if m.get("material")]
            if material_members and not event.get("material"):
                event["material"] = True
                event["_material_from_duplicate_ids"] = [
                    m.get("event_id") for m in material_members if m is not event
                ]

    material_events = [event for event in direct_events if event["material"]]
    material_events = _sorted_by_first_seen(material_events)

    # 官方来源逐条做显式反证检查：用**去重前**的直接官方事件，确保官方
    # 确认不会被“同标题的更早媒体副本”顺手去重掉。官方事件天然按重大处理。
    official_events = [
        event
        for event in direct_before_dedup
        if event["evidence_tier"] == "OFFICIAL_PRIMARY"
    ]
    invalidation_candidates: List[Dict[str, Any]] = []
    official_checks: List[Dict[str, Any]] = []
    seen_official_ids = set()
    for event in _sorted_by_first_seen(official_events):
        event_id = event.get("event_id")
        if event_id in seen_official_ids:
            continue
        seen_official_ids.add(event_id)
        evidence = _explicit_contradiction(event, str(trade.get("direction") or ""), terms)
        check = {
            "event_id": event.get("event_id"),
            "source_id": event.get("source_id"),
            "source_tier": event.get("source_tier"),
            "first_seen_at": event.get("first_seen_at"),
            "headline": event.get("headline"),
            "explicit_contradiction": bool(evidence),
        }
        if evidence:
            check["evidence"] = evidence
            invalidation_candidates.append({**check, "event": event})
        else:
            check["reason"] = (
                "official/primary source, but no explicit role-level or "
                "direction-adverse contradiction phrase linked to the symbol"
            )
        official_checks.append(check)

    if invalidation_candidates:
        primary = _sorted_by_first_seen(
            [candidate["event"] for candidate in invalidation_candidates]
        )[0]
        verdict = "INVALIDATE"
        verdict_reason = (
            "official/primary source contains an explicit contradiction phrase "
            f"({invalidation_candidates[0].get('evidence', {}).get('kind')})"
        )
    elif material_events:
        primary = sorted(
            material_events,
            key=lambda event: (-_severity(event), event["_first_seen_dt"], str(event["event_id"])),
        )[0]
        verdict = "REVIEW"
        verdict_reason = (
            "material direct event(s) exist, but none is an official/primary-source "
            "deterministic contradiction; unverified or media-only information "
            "cannot invalidate automatically"
        )
    else:
        primary = None
        verdict = "NO_CHANGE"
        verdict_reason = "no material direct event inside the holding window"

    first_material = material_events[0] if material_events else None

    trade_for_output = dict(trade)
    decision_metadata_raw = trade.get("metadata_json")
    decision_metadata, decision_metadata_error = _parse_json_object(decision_metadata_raw)

    event_window_sha = _sha256_obj(
        [_public_event(event) for event in included]
    )
    trade_row_sha = _sha256_obj(trade_for_output)
    evaluation_fingerprint = _sha256_obj(
        {
            "schema": SCHEMA,
            "trade_id": trade_id,
            "decision_id": actual_decision_id,
            "ticker": ticker,
            "entry_time": _iso_utc(entry_dt),
            "as_of": _iso_utc(as_of_dt),
            "aliases": terms,
            "materiality_threshold": materiality_threshold,
            "trade_row_sha256": trade_row_sha,
            "event_window_sha256": event_window_sha,
            "verdict": verdict,
            "primary_event_id": primary.get("event_id") if primary else None,
        }
    )

    counterfactual = {
        "status": "unavailable",
        "claimed": False,
        "avoided_pnl_usd": None,
        "reason": (
            "No event-window bars were loaded. Without intra-window prices around "
            "each event's first_seen_at, a counterfactual exit price and avoided "
            "PnL cannot be computed. This artifact must not be read as claiming "
            "that an exit would have avoided or improved any loss."
        ),
        "required_inputs_for_future_evaluation": [
            "event-window OHLCV bars covering entry_time..as_of from a recorded source",
            "bar timestamps aligned to the same UTC clock as first_seen_at",
            "explicit exit rule and cost model (fees/slippage) pre-registered before scoring",
        ],
    }

    result: Dict[str, Any] = {
        "schema": SCHEMA,
        "mode": "offline_read_only",
        "production_write": False,
        "auto_action": False,
        "description": (
            "EXT in-position event reassessment shadow; record-only evaluation "
            "of one closed trade using EventStore first_seen_at."
        ),
        "script": "scripts/ext_event_risk_shadow.py",
        "script_sha256": _sha256_file(Path(__file__).resolve()),
        "decision_id": actual_decision_id,
        "trade_id": trade_id,
        "ticker": ticker,
        "aliases": terms,
        "as_of": _iso_utc(as_of_dt),
        "as_of_source": as_of_source,
        "window": {
            "entry_time_utc": _iso_utc(entry_dt),
            "as_of_utc": _iso_utc(as_of_dt),
            "rule": "first_seen_at > entry_time AND first_seen_at <= as_of",
            "timezone": "UTC",
        },
        "db_paths": {
            "trade_db": str(Path(trade_db).expanduser().resolve()),
            "event_db": str(Path(event_db).expanduser().resolve()),
        },
        "input_hashes": {
            "trade_db_file_sha256": _sha256_file(Path(trade_db).expanduser().resolve()),
            "event_db_file_sha256": _sha256_file(Path(event_db).expanduser().resolve()),
            "db_file_hash_scope": "main .db file only; SQLite WAL is not included",
            "trade_row_sha256": trade_row_sha,
            "event_window_rows_sha256": event_window_sha,
        },
        "evaluation_sha256": evaluation_fingerprint,
        "verdict": verdict,
        "verdict_reason": verdict_reason,
        "assessment_event": _public_event(primary) if primary else None,
        "first_material_event": _public_event(first_material) if first_material else None,
        "material_event_count": len(material_events),
        "material_events": [_public_event(event) for event in material_events],
        "dedup": {
            "direct_events_before_dedup": len(direct_before_dedup),
            "direct_events_after_dedup": len(direct_events),
            "duplicates_removed": len(duplicates),
            "duplicates": duplicates,
        },
        "window_audit": {
            "canonical_events_scanned": len(all_events),
            "included_in_window": len(included),
            "excluded_before_or_at_entry": excluded_before_or_at_entry,
            "excluded_after_as_of": excluded_after_as_of,
            "excluded_unparseable_first_seen": excluded_unparseable,
        },
        "contradiction_check": {
            "official_primary_events_examined": official_checks,
            "invalidation_candidates": [
                {
                    "event_id": candidate["event_id"],
                    "source_id": candidate["source_id"],
                    "source_tier": candidate["source_tier"],
                    "first_seen_at": candidate["first_seen_at"],
                    "evidence": candidate.get("evidence"),
                }
                for candidate in invalidation_candidates
            ],
            "invalidate_rule": (
                "INVALIDATE requires source_tier T0 / SEC / company-IR primary "
                "source AND an explicit symbol-linked contradiction phrase in "
                "headline or summary. Media reports, RSS discovery items and "
                "social posts can never trigger INVALIDATE."
            ),
        },
        "counterfactual": counterfactual,
        "trade_record": trade_for_output,
        "decision_metadata": decision_metadata,
        "decision_metadata_parse_warning": decision_metadata_error,
        "decision_metadata_sha256": _sha256_text(_stable_json(decision_metadata))
        if decision_metadata
        else None,
        "limitations": [
            "first_seen_at is collection/visibility time, not publication time; "
            "an event that was published earlier but ingested later is only "
            "visible from first_seen_at onward.",
            "canonical_events.status is UNVERIFIED for every source in the current "
            "store, including SEC filings; evidence tier is therefore derived "
            "from source_tier/source_id/event_type, not from the status column.",
            "relevance is lexical/entity-based (ticker, known aliases, "
            "event_meta.entities, headline/summary/facts); it is not semantic "
            "understanding and can have false positives and false negatives.",
            "materiality uses event_meta.materiality >= threshold for non-official "
            "sources; official/primary events are treated as material by source "
            "class. This is a pre-registered shadow rule, not a production rule.",
            "Only one closed trade is evaluated here. Single-case output is not "
            "statistical evidence of edge, safety, or improvement.",
            "No event-window bars were loaded; counterfactual PnL is unavailable "
            "and no avoided-PnL claim is made.",
            "No cron, registry entry, production consumer, order action or live "
            "hook exists for this shadow. Any live hook requires separate RFC and "
            "explicit user approval.",
        ],
    }

    payload_for_hash = {
        key: value
        for key, value in result.items()
        if key not in {"payload_sha256", "output_path"}
    }
    result["payload_sha256"] = _sha256_obj(payload_for_hash)
    return result


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


def _default_output_path(result: Dict[str, Any], out_dir: Path) -> Path:
    decision_slug = re.sub(
        r"[^A-Za-z0-9._-]+", "-", str(result.get("decision_id") or "unknown")
    ).strip("-")
    return out_dir / (
        f"ext-risk-{result['trade_id']}-{decision_slug}-"
        f"{result['evaluation_sha256'][:12]}.json"
    )


def _write_new_file(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(path, flags, 0o644)
    except FileExistsError as exc:
        raise FileExistsError(
            f"refusing to overwrite existing output: {path} "
            "(choose a new --out path or remove the old artifact deliberately)"
        ) from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
    except BaseException:
        # 文件已创建但写入失败：删除半成品，避免留下可被误读的残缺产物。
        try:
            path.unlink()
        except OSError as cleanup_error:
            print(
                f"warning: failed to remove partial artifact {path}: "
                f"{type(cleanup_error).__name__}: {cleanup_error}",
                file=sys.stderr,
            )
        raise


def _print_summary(result: Dict[str, Any], artifact_path: Path) -> None:
    primary = result.get("assessment_event") or {}
    print(f"verdict: {result['verdict']}  ({result['verdict_reason']})")
    print(f"trade: id={result['trade_id']} decision_id={result['decision_id']}")
    print(f"window: {result['window']['entry_time_utc']} -> {result['as_of']}")
    print(
        "events: "
        f"scanned={result['window_audit']['canonical_events_scanned']} "
        f"in_window={result['window_audit']['included_in_window']} "
        f"direct_after_dedup={result['dedup']['direct_events_after_dedup']} "
        f"material={result['material_event_count']}"
    )
    if primary:
        print(
            "assessment_event: "
            f"{primary.get('event_id')} first_seen={primary.get('first_seen_at')} "
            f"tier={primary.get('source_tier')} source={primary.get('source_id')}"
        )
        print(f"  headline: {primary.get('headline')}")
    counterfactual = result.get("counterfactual") or {}
    print(f"counterfactual: {counterfactual.get('status')} (claimed=false)")
    print(f"artifact: {artifact_path}")
    print("production_write: false")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Offline, read-only EXT in-position event reassessment shadow (RFC-205)."
    )
    parser.add_argument("--trade-id", type=int, default=DEFAULT_TRADE_ID)
    parser.add_argument("--decision-id", default=DEFAULT_DECISION_ID)
    parser.add_argument("--trade-db", type=Path, default=DEFAULT_TRADE_DB)
    parser.add_argument("--event-db", type=Path, default=DEFAULT_EVENT_DB)
    parser.add_argument(
        "--as-of",
        default=None,
        help="UTC ISO timestamp; default = trade_records.exit_time",
    )
    parser.add_argument(
        "--alias",
        action="append",
        default=[],
        help="extra symbol alias (repeatable), e.g. --alias 'NVIDIA Corp'",
    )
    parser.add_argument(
        "--materiality-threshold",
        type=float,
        default=MATERIALITY_THRESHOLD,
        help="event_meta.materiality threshold for non-official sources (default 0.40)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="artifact path; default under data/shadow/ext_event_risk/; existing files are refused",
    )
    parser.add_argument(
        "--print-json",
        action="store_true",
        help="also print the full artifact JSON to stdout",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="suppress the human-readable summary (artifact is still written)",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = evaluate(
            trade_db=args.trade_db,
            event_db=args.event_db,
            trade_id=args.trade_id,
            decision_id=args.decision_id,
            as_of_override=args.as_of,
            extra_aliases=args.alias,
            materiality_threshold=args.materiality_threshold,
        )
    except (FileNotFoundError, LookupError, ValueError, sqlite3.Error) as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    output_path = (
        args.out
        if args.out is not None
        else _default_output_path(result, DEFAULT_OUT_DIR)
    )
    result["output_path"] = str(output_path)

    try:
        _write_new_file(output_path, result)
    except FileExistsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: failed to write artifact: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    if args.print_json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    elif not args.quiet:
        _print_summary(result, output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())