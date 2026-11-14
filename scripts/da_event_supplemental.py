#!/usr/bin/env python3
"""Offline supplemental evidence from frozen DA raw checkpoints (DEB §65.4 P0.1).

Why: EventStore direct peer coverage on 2026-11-12 was NVDA-only, while the raw DA
checkpoints already contained explicit AMD/AVGO replacement reports. This module
turns those raw `news` / `news_v2` items into a reproducible, grounded evidence set
for the shared brief, without touching production or inventing timestamps.

Guarantees:
  - every included item has ``first_seen_at`` (actual raw collection time) <= cutoff;
  - every included item has ``publish_time`` <= cutoff when an exact time exists;
  - date-only items keep ``event_time`` as a date string / precision ``DATE_ONLY``,
    and never get a fabricated midnight timestamp;
  - stable IDs ``SUP-<sha256(normalized title|url + event date)[:16]>``;
  - dedup across the 8 checkpoints by normalized title (fallback URL), retaining
    every occurrence as a member fact with source symbol + raw path + raw SHA256;
  - no production imports, no network, no LLM calls.

Consumed by ``scripts/da_event_intelligence.py brief`` and
``scripts/da_event_case_freeze.py``; both are offline.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc
ET = ZoneInfo("America/New_York")
CASE_SYMBOLS = ("NVDA", "AMD", "AVGO", "TSM", "MU", "SNDK", "ARM", "NBIS")

_SYMBOL_RULES: Dict[str, Sequence[tuple[str, int]]] = {
    "NVDA": (("nvidia", re.IGNORECASE), ("NVDA", 0), ("英伟达", 0)),
    "AMD": (("Advanced Micro Devices", re.IGNORECASE), ("AMD", 0), ("超威半导体", 0)),
    "AVGO": (("Broadcom", re.IGNORECASE), ("AVGO", 0), ("博通", 0)),
    "TSM": (
        ("Taiwan Semiconductor", re.IGNORECASE), ("TSMC", re.IGNORECASE),
        ("TSM", 0), ("台积电", 0),
    ),
    "MU": (("Micron", re.IGNORECASE), ("MU", 0), ("美光", 0)),
    "SNDK": (("SanDisk", re.IGNORECASE), ("SNDK", 0), ("闪迪", 0)),
    "ARM": (("Arm Holdings", re.IGNORECASE), ("ARM", 0), ("安谋", 0)),
    "NBIS": (("Nebius", re.IGNORECASE), ("NBIS", 0)),
}

_NVDA_PATTERN = re.compile(r"nvidia|NVDA|Huang|英伟达", re.IGNORECASE)
_T1_PUBLISHERS = frozenset({
    "reuters", "bloomberg", "wsj", "wall street journal", "barrons", "barron's",
    "cnbc", "yahoo", "marketwatch", "financial times", "ft", "ap", "associated press",
    "the information", "axios", "politico", "business insider", "forbes",
})
_T2_PUBLISHERS = frozenset({
    "futunn", "google news", "chartmill", "kucoin", "bloomingbit", "digitimes",
    "the tech buzz", "biggo finance", "the daily upside", "semafor", "quartz",
})
_TRACKING_QUERY_KEYS = frozenset({
    "oc", "futusource", "src", "ref", "source", "cmpid", "taid", "mod",
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
})

_DATE_ONLY_RE = re.compile(r"^(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})$")
_DATE_ONLY_COMPACT_RE = re.compile(r"^(?P<year>\d{4})(?P<month>\d{2})(?P<day>\d{2})$")
_MONTH_DAY_RE = re.compile(r"^(?P<month>\d{2})-(?P<day>\d{2})$")


def _compile(rule: str, flags: int) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(rule)}(?![A-Za-z0-9])", flags)


_SYMBOL_PATTERNS = {symbol: [_compile(rule, flags) for rule, flags in rules]
                    for symbol, rules in _SYMBOL_RULES.items()}


def strip_html(value: Any) -> str:
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def normalize_title(value: Any) -> str:
    text = strip_html(value).casefold()
    return re.sub(r"[^0-9a-z一-鿿]+", " ", text).strip()


def normalize_url(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parts = urlsplit(raw)
    query = [
        (key, item) for key, item in parse_qsl(parts.query, keep_blank_values=True)
        if key.casefold() not in _TRACKING_QUERY_KEYS
    ]
    return urlunsplit((
        parts.scheme.casefold(),
        parts.netloc.casefold(),
        parts.path.rstrip("/"),
        urlencode(query),
        "",
    ))


def symbol_mentioned(text: str, symbol: str) -> bool:
    bare = symbol.upper().removeprefix("US.")
    patterns = _SYMBOL_PATTERNS.get(bare)
    if patterns is None:
        raise KeyError(f"unknown case symbol: {symbol}")
    return any(pattern.search(text or "") for pattern in patterns)


def mentioned_symbols(text: str, case_symbols: Sequence[str] = CASE_SYMBOLS) -> List[str]:
    return [symbol for symbol in case_symbols if symbol_mentioned(text, symbol)]


def _parse_iso(value: str) -> Tuple[datetime, bool]:
    text = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    assumed_utc = parsed.tzinfo is None
    if assumed_utc:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC), assumed_utc


def parse_event_time(
    item: Dict[str, Any],
    *,
    collection_dt: datetime,
) -> Dict[str, Any]:
    """Parse publish time without inventing a clock for date-only values.

    Returns ``event_time`` (ISO exact, ``YYYY-MM-DD`` for date-only, or None),
    ``event_time_precision`` in SECOND/MINUTE/DATE_ONLY/UNKNOWN, and
    ``event_date`` for date-only no-future checks.
    """
    raw_value = item.get("publish_time")
    if raw_value in (None, ""):
        raw_value = item.get("time")
    if raw_value in (None, ""):
        return {
            "event_time": None, "event_time_precision": "UNKNOWN",
            "event_date": None, "event_time_raw": None,
            "event_time_assumed_utc": False,
        }
    text = str(raw_value).strip()

    if re.fullmatch(r"\d+", text):
        if len(text) == 8 and text.startswith("20"):
            compact = _DATE_ONLY_COMPACT_RE.match(text)
            if compact:
                event_date = date(int(compact.group("year")), int(compact.group("month")),
                                  int(compact.group("day"))).isoformat()
                return {
                    "event_time": event_date, "event_time_precision": "DATE_ONLY",
                    "event_date": event_date, "event_time_raw": text,
                    "event_time_assumed_utc": False,
                }
        if len(text) >= 10:
            parsed = datetime.fromtimestamp(float(text), UTC)
            return {
                "event_time": parsed.isoformat(), "event_time_precision": "SECOND",
                "event_date": parsed.date().isoformat(), "event_time_raw": text,
                "event_time_assumed_utc": False,
            }

    date_match = _DATE_ONLY_RE.match(text)
    if date_match:
        event_date = date(
            int(date_match.group("year")), int(date_match.group("month")),
            int(date_match.group("day")),
        ).isoformat()
        return {
            "event_time": event_date, "event_time_precision": "DATE_ONLY",
            "event_date": event_date, "event_time_raw": text,
            "event_time_assumed_utc": False,
        }
    month_day = _MONTH_DAY_RE.match(text)
    if month_day:
        event_date = date(
            collection_dt.year, int(month_day.group("month")), int(month_day.group("day")),
        ).isoformat()
        return {
            "event_time": event_date, "event_time_precision": "DATE_ONLY",
            "event_date": event_date, "event_time_raw": text,
            "event_time_assumed_utc": False,
        }

    try:
        parsed, assumed_utc = _parse_iso(text)
    except ValueError:
        return {
            "event_time": None, "event_time_precision": "UNKNOWN",
            "event_date": None, "event_time_raw": text,
            "event_time_assumed_utc": False,
        }
    precision = "SECOND" if parsed.second or "." in text else "MINUTE"
    return {
        "event_time": parsed.isoformat(), "event_time_precision": precision,
        "event_date": parsed.date().isoformat(), "event_time_raw": text,
        "event_time_assumed_utc": assumed_utc,
    }


def _collection_time(checkpoint: Dict[str, Any]) -> Tuple[datetime, str]:
    collected = checkpoint.get("collected") or {}
    raw = collected.get("_timestamp")
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(float(raw), UTC), "collected._timestamp"
    if raw:
        parsed, _ = _parse_iso(str(raw))
        return parsed, "collected._timestamp"
    saved_at = checkpoint.get("saved_at")
    if saved_at:
        parsed, _ = _parse_iso(str(saved_at))
        return parsed, "saved_at"
    raise ValueError("raw checkpoint has neither collected._timestamp nor saved_at")


def _source_tier(kind: str, item: Dict[str, Any], title: str) -> str:
    publisher = str(item.get("source") or "").casefold()
    lowered_title = title.casefold()
    if publisher and any(name in publisher for name in _T1_PUBLISHERS):
        return "T1"
    if any(name in lowered_title for name in ("barrons", "barron's", "wsj", "reuters", "bloomberg", "cnbc")):
        return "T1"
    if publisher and any(name in publisher for name in _T2_PUBLISHERS):
        return "T2"
    return "T2"


def _publisher(kind: str, item: Dict[str, Any], title: str) -> str:
    supplied = strip_html(item.get("source"))
    if supplied:
        return supplied
    if kind == "news":
        return "futunn"
    lowered_title = title.casefold()
    for name in ("barrons", "wsj", "reuters", "bloomberg", "cnbc", "yahoo"):
        if name in lowered_title:
            return name
    return "unknown"


def stable_evidence_id(title: str, url: str, event_date: Optional[str]) -> Optional[str]:
    key = normalize_title(title) or normalize_url(url)
    if not key:
        return None
    material = f"{key}|{event_date or ''}"
    return "SUP-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative(path: Path, repo_root: Path) -> str:
    try:
        return str(path.resolve().relative_to(repo_root))
    except ValueError:
        return str(path)


def _materiality(item: Dict[str, Any]) -> float:
    if item["nvda_related"] and len(item["mentioned_symbols"]) >= 2:
        return 0.60
    if len(item["mentioned_symbols"]) >= 2 or len(item["source_symbols"]) >= 2:
        return 0.50
    if item["nvda_related"]:
        return 0.45
    return 0.35


def _item_record(
    *,
    kind: str,
    item: Dict[str, Any],
    source_symbol: str,
    raw_path: str,
    raw_sha256: str,
    collection_dt: datetime,
    collection_source: str,
    cutoff_dt: datetime,
    case_symbols: Sequence[str],
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    title = strip_html(item.get("title"))
    summary = strip_html(item.get("summary"))[:500]
    url = str(item.get("url") or "").strip()
    text = f"{title} {summary}"
    mentioned = mentioned_symbols(text, case_symbols)
    nvda_related = bool(_NVDA_PATTERN.search(text))
    if not mentioned and not nvda_related:
        return None, None
    timing = parse_event_time(item, collection_dt=collection_dt)
    first_seen_at = collection_dt.isoformat()
    no_future = collection_dt <= cutoff_dt
    if timing["event_time_precision"] == "DATE_ONLY":
        event_date = timing["event_date"]
        no_future = no_future and bool(event_date) and date.fromisoformat(event_date) <= cutoff_dt.astimezone(ET).date()
    elif timing["event_time"]:
        parsed, _ = _parse_iso(timing["event_time"])
        no_future = no_future and parsed <= cutoff_dt
    if not no_future:
        return None, {
            "reason": "future_publish_or_collection",
            "headline": title[:180],
            "publish_time": timing["event_time_raw"],
            "event_time": timing["event_time"],
            "event_time_precision": timing["event_time_precision"],
            "first_seen_at": first_seen_at,
            "source_symbol": source_symbol,
            "raw_path": raw_path,
        }
    evidence_id = stable_evidence_id(title, url, timing["event_date"])
    if evidence_id is None:
        return None, {
            "reason": "missing_title_and_url",
            "headline": title[:180],
            "first_seen_at": first_seen_at,
            "source_symbol": source_symbol,
            "raw_path": raw_path,
        }
    publisher = _publisher(kind, item, title)
    return {
        "evidence_id": evidence_id,
        "headline": title,
        "summary": summary,
        "url": url,
        "normalized_url": normalize_url(url),
        "event_time": timing["event_time"],
        "event_time_precision": timing["event_time_precision"],
        "event_date": timing["event_date"],
        "event_time_raw": timing["event_time_raw"],
        "event_time_assumed_utc": timing["event_time_assumed_utc"],
        "first_seen_at": first_seen_at,
        "first_seen_source": collection_source,
        "source_id": f"DA_RAW_{kind.upper()}",
        "source_tier": _source_tier(kind, item, title),
        "publisher": publisher,
        "status": "UNVERIFIED",
        "source_symbol": source_symbol,
        "raw_path": raw_path,
        "raw_sha256": raw_sha256,
        "mentioned_symbols": mentioned,
        "nvda_related": nvda_related,
    }, None


def extract_supplemental_evidence(
    raw_root: Path,
    *,
    cutoff: str,
    case_symbols: Sequence[str] = CASE_SYMBOLS,
    repo_root: Path = ROOT,
) -> Dict[str, Any]:
    """Extract, dedup and validate supplemental evidence from raw DA checkpoints."""
    cutoff_dt = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
    if cutoff_dt.tzinfo is None:
        raise ValueError(f"supplemental cutoff requires timezone: {cutoff!r}")
    cutoff_dt = cutoff_dt.astimezone(UTC)
    case_symbols = tuple(sorted({
        str(symbol).upper().removeprefix("US.") for symbol in case_symbols
    }))
    raw_root = Path(raw_root)
    checkpoints = []
    records: List[Dict[str, Any]] = []
    rejected = []
    for symbol in case_symbols:
        raw_path = raw_root / f"US.{symbol}.raw.json"
        if not raw_path.exists():
            raise FileNotFoundError(f"missing raw checkpoint: {raw_path}")
        checkpoint = json.loads(raw_path.read_text(encoding="utf-8"))
        collection_dt, collection_source = _collection_time(checkpoint)
        raw_sha256 = _sha256_file(raw_path)
        relative = _relative(raw_path, repo_root)
        collected = checkpoint.get("collected") or {}
        checkpoints.append({
            "symbol": f"US.{symbol}",
            "trade_date": checkpoint.get("trade_date"),
            "saved_at": checkpoint.get("saved_at"),
            "collected_at": collection_dt.isoformat(),
            "collected_at_source": collection_source,
            "path": relative,
            "sha256": raw_sha256,
            "bytes": raw_path.stat().st_size,
            "user_prompt_sha256": hashlib.sha256(
                str(checkpoint.get("user_prompt") or "").encode("utf-8")
            ).hexdigest() if checkpoint.get("user_prompt") else None,
            "user_prompt_chars": len(str(checkpoint.get("user_prompt") or "")),
            "news_item_count": sum(len(collected.get(key) or []) for key in ("news", "news_v2")),
        })
        for kind in ("news", "news_v2"):
            for item in collected.get(kind) or []:
                if not isinstance(item, dict):
                    continue
                record, rejection = _item_record(
                    kind=kind, item=item, source_symbol=f"US.{symbol}",
                    raw_path=relative, raw_sha256=raw_sha256,
                    collection_dt=collection_dt, collection_source=collection_source,
                    cutoff_dt=cutoff_dt, case_symbols=case_symbols,
                )
                if rejection is not None:
                    rejected.append(rejection)
                if record is not None:
                    records.append(record)

    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for record in records:
        grouped.setdefault(record["evidence_id"], []).append(record)

    items = []
    duplicates_collapsed = 0
    date_only_items = 0
    unknown_time_items = 0
    for evidence_id, members in sorted(grouped.items()):
        precision_rank = {"SECOND": 3, "MINUTE": 2, "DATE_ONLY": 1, "UNKNOWN": 0}
        members = sorted(
            members,
            key=lambda row: (
                -precision_rank.get(row["event_time_precision"], 0),
                row["first_seen_at"],
                row["raw_path"],
            ),
        )
        unique_members = []
        seen_members = set()
        for member in members:
            key = (
                member["source_symbol"], member["raw_path"], member["url"],
                member["event_time"], member["first_seen_at"],
            )
            if key in seen_members:
                continue
            seen_members.add(key)
            unique_members.append(member)
        duplicates_collapsed += max(0, len(members) - 1)
        primary = unique_members[0]
        earliest = min(unique_members, key=lambda member: member["first_seen_at"])
        source_symbols = sorted({member["source_symbol"] for member in unique_members})
        publishers = sorted({member["publisher"] for member in unique_members})
        source_ids = sorted({member["source_id"] for member in unique_members})
        urls = sorted({member["url"] for member in unique_members if member["url"]})
        mentioned = sorted({symbol for member in unique_members for symbol in member["mentioned_symbols"]})
        event_date = primary["event_date"]
        item = {
            "evidence_id": evidence_id,
            "headline": primary["headline"],
            "summary": primary["summary"],
            "event_time": primary["event_time"],
            "event_time_precision": primary["event_time_precision"],
            "event_date": event_date,
            "first_seen_at": earliest["first_seen_at"],
            "first_seen_source": earliest["first_seen_source"],
            "source_id": primary["source_id"],
            "source_tier": primary["source_tier"],
            "status": "UNVERIFIED",
            "publishers": publishers,
            "source_ids": source_ids,
            "urls": urls,
            "source_symbols": source_symbols,
            "source_symbol_count": len(source_symbols),
            "raw_paths": sorted({member["raw_path"] for member in unique_members}),
            "raw_sha256s": sorted({member["raw_sha256"] for member in unique_members}),
            "mentioned_symbols": mentioned,
            "nvda_related": any(member["nvda_related"] for member in unique_members),
            "member_facts": [
                {
                    "event_id": evidence_id,
                    "event_time": member["event_time"],
                    "event_time_precision": member["event_time_precision"],
                    "first_seen_at": member["first_seen_at"],
                    "headline": member["headline"],
                    "source_id": member["source_id"],
                    "source_url": member["url"],
                    "status": "UNVERIFIED",
                    "source_symbol": member["source_symbol"],
                    "raw_path": member["raw_path"],
                    "raw_sha256": member["raw_sha256"],
                    "publisher": member["publisher"],
                }
                for member in unique_members
            ],
        }
        item["materiality"] = _materiality(item)
        items.append(item)
        if item["event_time_precision"] == "DATE_ONLY":
            date_only_items += 1
        if item["event_time_precision"] == "UNKNOWN":
            unknown_time_items += 1
    items.sort(key=lambda row: (-row["materiality"], -row["source_symbol_count"],
                                row["first_seen_at"], row["evidence_id"]))
    evidence_hash = hashlib.sha256(json.dumps(
        items, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    return {
        "schema": "da_supplemental_evidence_v1",
        "mode": "OFFLINE_RAW_CHECKPOINTS",
        "cutoff": cutoff_dt.isoformat(),
        "cutoff_et": cutoff_dt.astimezone(ET).isoformat(),
        "case_symbols": [f"US.{symbol}" for symbol in case_symbols],
        "checkpoints": checkpoints,
        "stats": {
            "raw_item_count": sum(row["news_item_count"] for row in checkpoints),
            "relevant_item_count": len(records) + len(rejected),
            "unique_evidence_count": len(items),
            "duplicates_collapsed": duplicates_collapsed,
            "date_only_item_count": date_only_items,
            "unknown_time_item_count": unknown_time_items,
            "rejected_future_count": len([row for row in rejected if row["reason"] == "future_publish_or_collection"]),
            "rejected_missing_identity_count": len([row for row in rejected if row["reason"] == "missing_title_and_url"]),
            "source_symbol_counts": {
                f"US.{symbol}": sum(1 for item in items if f"US.{symbol}" in item["source_symbols"])
                for symbol in case_symbols
            },
            "mentioned_symbol_counts": {
                f"US.{symbol}": sum(1 for item in items if symbol in item["mentioned_symbols"])
                for symbol in case_symbols
            },
        },
        "items": items,
        "rejected": rejected,
        "evidence_hash": evidence_hash,
        "llm_calls": 0,
    }


def evidence_to_facts(items: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    facts = []
    for item in items:
        facts.append({
            "event_id": item["evidence_id"],
            "member_event_ids": [item["evidence_id"]],
            "event_time": item["event_time"],
            "event_time_precision": item["event_time_precision"],
            "first_seen_at": item["first_seen_at"],
            "first_seen_source": item["first_seen_source"],
            "source_tier": item["source_tier"],
            "status": item["status"],
            "source_ids": item["source_ids"],
            "corroborating_publishers": item["publishers"],
            "source_urls": item["urls"],
            "event_type": "DA_RAW_NEWS",
            "materiality": item["materiality"],
            "headline": item["headline"],
            "summary": item["summary"],
            "entities": {"symbols": item["mentioned_symbols"]},
            "member_facts": item["member_facts"],
            "supplemental": True,
            "evidence_id": item["evidence_id"],
            "source_symbols": item["source_symbols"],
            "raw_paths": item["raw_paths"],
            "raw_sha256s": item["raw_sha256s"],
        })
    return facts


def validate_supplemental_evidence(
    result: Dict[str, Any],
    *,
    cutoff: str,
    case_symbols: Sequence[str] = CASE_SYMBOLS,
) -> List[str]:
    errors = []
    cutoff_dt = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
    if cutoff_dt.tzinfo is None:
        errors.append("cutoff_missing_timezone")
        return errors
    cutoff_dt = cutoff_dt.astimezone(UTC)
    if result.get("cutoff") != cutoff_dt.isoformat():
        errors.append("cutoff_mismatch")
    allowed_symbols = {f"US.{symbol}" for symbol in case_symbols}
    seen_ids = set()
    for item in result.get("items") or []:
        evidence_id = str(item.get("evidence_id") or "")
        if not evidence_id.startswith("SUP-"):
            errors.append(f"invalid_evidence_id:{evidence_id}")
        if evidence_id in seen_ids:
            errors.append(f"duplicate_evidence_id:{evidence_id}")
        seen_ids.add(evidence_id)
        if not item.get("headline"):
            errors.append(f"missing_headline:{evidence_id}")
        first_seen = item.get("first_seen_at")
        try:
            if _parse_iso(str(first_seen))[0] > cutoff_dt:
                errors.append(f"future_first_seen:{evidence_id}")
        except (TypeError, ValueError):
            errors.append(f"invalid_first_seen:{evidence_id}")
        precision = item.get("event_time_precision")
        if precision == "DATE_ONLY":
            event_time = str(item.get("event_time") or "")
            if "T" in event_time:
                errors.append(f"date_only_has_clock:{evidence_id}")
            try:
                if date.fromisoformat(event_time) > cutoff_dt.astimezone(ET).date():
                    errors.append(f"future_event_date:{evidence_id}")
            except ValueError:
                errors.append(f"invalid_date_only:{evidence_id}")
        elif precision == "UNKNOWN":
            if item.get("event_time") is not None:
                errors.append(f"unknown_precision_has_time:{evidence_id}")
        else:
            try:
                if _parse_iso(str(item.get("event_time")))[0] > cutoff_dt:
                    errors.append(f"future_event_time:{evidence_id}")
            except (TypeError, ValueError):
                errors.append(f"invalid_event_time:{evidence_id}")
        if item.get("source_tier") not in {"T0", "T1", "T2"}:
            errors.append(f"invalid_source_tier:{evidence_id}")
        if item.get("status") != "UNVERIFIED":
            errors.append(f"invalid_status:{evidence_id}")
        source_symbols = list(item.get("source_symbols") or [])
        if not source_symbols:
            errors.append(f"missing_source_symbol:{evidence_id}")
        for symbol in source_symbols:
            if symbol not in allowed_symbols:
                errors.append(f"unknown_source_symbol:{evidence_id}:{symbol}")
        if not item.get("raw_paths") or not item.get("raw_sha256s"):
            errors.append(f"missing_raw_provenance:{evidence_id}")
        for member in item.get("member_facts") or []:
            try:
                if _parse_iso(str(member.get("first_seen_at")))[0] > cutoff_dt:
                    errors.append(f"future_member_first_seen:{evidence_id}")
            except (TypeError, ValueError):
                errors.append(f"invalid_member_first_seen:{evidence_id}")
            if not member.get("raw_path") or not member.get("raw_sha256"):
                errors.append(f"missing_member_provenance:{evidence_id}")
    return sorted(set(errors))


def merge_supplemental_into_context(
    context: Dict[str, Any],
    result: Dict[str, Any],
    *,
    cutoff: str,
    limit: int = 0,
    case_symbols: Sequence[str] = CASE_SYMBOLS,
) -> Dict[str, Any]:
    """Append supplemental facts to a brief context, fail-closed on grounding/future."""
    if int(limit) < 0:
        raise ValueError("supplemental limit must be >= 0")
    errors = validate_supplemental_evidence(result, cutoff=cutoff, case_symbols=case_symbols)
    if errors:
        raise ValueError(f"supplemental evidence grounding failed: {errors}")
    future_rejections = [
        row for row in result.get("rejected") or []
        if row.get("reason") == "future_publish_or_collection"
    ]
    if future_rejections:
        raise ValueError(f"supplemental evidence contains future items: {future_rejections[:3]}")
    if result.get("cutoff") != context.get("as_of"):
        raise ValueError(
            f"shared cutoff mismatch: supplemental={result.get('cutoff')} context={context.get('as_of')}"
        )
    facts = evidence_to_facts(result.get("items") or [])
    existing_ids = {fact["event_id"] for fact in context.get("facts") or []}
    collisions = sorted(existing_ids.intersection(fact["event_id"] for fact in facts))
    if collisions:
        raise ValueError(f"supplemental evidence id collision: {collisions[:3]}")
    selected = facts
    omitted = []
    if limit > 0 and len(facts) > limit:
        selected = facts[:limit]
        omitted = facts[limit:]
    context["facts"] = list(context.get("facts") or []) + selected
    context["supplemental"] = {
        "schema": "da_supplemental_context_v1",
        "cutoff": result["cutoff"],
        "cutoff_et": result["cutoff_et"],
        "evidence_hash": result["evidence_hash"],
        "total_evidence_count": len(facts),
        "merged_fact_count": len(selected),
        "omitted_fact_count": len(omitted),
        "omitted_evidence_ids": [fact["event_id"] for fact in omitted],
        "date_only_item_count": result["stats"]["date_only_item_count"],
        "unknown_time_item_count": result["stats"]["unknown_time_item_count"],
        "source_checkpoints": result["checkpoints"],
        "stats": result["stats"],
    }
    unknowns = set(context.get("unknowns") or [])
    unknowns.add(f"supplemental_evidence_from_da_raw_checkpoints:{len(selected)}")
    if omitted:
        unknowns.add(f"supplemental_unreviewed_evidence_due_to_prompt_budget:{len(omitted)}")
    if result["stats"]["date_only_item_count"]:
        unknowns.add(f"supplemental_date_only_items:{result['stats']['date_only_item_count']}")
    if result["stats"]["unknown_time_item_count"]:
        unknowns.add(f"supplemental_unknown_time_items:{result['stats']['unknown_time_item_count']}")
    context["unknowns"] = sorted(unknowns)
    return context