from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.event_stream.models import (
    CanonicalEvent,
    RawEvent,
    SourceResult,
    make_event_id,
    make_raw_id,
)

SOURCE_ID = "SEC_FILINGS_PROVIDER"
SOURCE_TIER = "T0"
PROVIDER_SOURCE = "sec_8k_provider + sec_form4_provider"


def _event_from_row(row: Dict[str, Any], form: str) -> CanonicalEvent:
    ticker = str(row.get("ticker") or "").strip().upper().removeprefix("US.")
    company = str(row.get("company") or ticker or "UNKNOWN").strip()
    form = str(form or "8-K").strip().upper()
    updated = str(row.get("updated") or "").strip() or None
    items = row.get("items") if isinstance(row.get("items"), list) else []
    item_labels = row.get("item_labels") if isinstance(row.get("item_labels"), list) else []
    if form in ("4", "3", "5"):
        event_type = "SEC_FORM4_FILING"
        headline = f"Form {form} - {company} ({ticker})"
        buy_count = int(row.get("buy_count") or 0)
        sell_count = int(row.get("sell_count") or 0)
        net_buy = row.get("net_buy_amount")
        summary = (
            f"Form {form} insider activity: buy {buy_count} / sell {sell_count}"
            + (f", net ${float(net_buy):,.0f}" if isinstance(net_buy, (int, float)) else "")
        )
    elif form.startswith("SC 13"):
        event_type = "SEC_13DG_FILING"
        headline = f"{form} - {company} ({ticker})"
        summary = f"{form} subject filing"
    else:
        event_type = "SEC_8K_FILING"
        headline = f"{form or '8-K'} - {company} ({ticker})"
        summary = "; ".join(str(x) for x in item_labels) or "8-K filing"
    url = str(row.get("url") or "").strip()
    return CanonicalEvent(
        event_id=make_event_id(SOURCE_ID, url, headline, updated),
        source_id=SOURCE_ID,
        source_tier=SOURCE_TIER,
        source_url=url,
        event_type=event_type,
        event_time=updated,
        headline=headline,
        summary=summary[:500],
        facts={
            "source": PROVIDER_SOURCE,
            "form": form,
            "ticker": ticker,
            "company": company,
            "items": items,
            "item_labels": item_labels,
            "transactions": row.get("transactions") or [],
            "buy_count": row.get("buy_count"),
            "sell_count": row.get("sell_count"),
            "net_buy_amount": row.get("net_buy_amount"),
        },
        status="UNVERIFIED",
        confidence=0.85,
        reasoning_version="v0",
    )


def build_provider_events(
    rows_8k: List[Dict[str, Any]],
    rows_13dg: List[Dict[str, Any]],
    rows_form4: Optional[List[Dict[str, Any]]] = None,
) -> List[CanonicalEvent]:
    """纯函数: provider 结果 → canonical events (去重 by event_id)."""
    events: List[CanonicalEvent] = []
    seen = set()
    for row in rows_8k:
        if not isinstance(row, dict):
            continue
        event = _event_from_row(row, "8-K")
        if event.event_id not in seen:
            seen.add(event.event_id)
            events.append(event)
    for row in rows_13dg:
        if not isinstance(row, dict):
            continue
        form = str(row.get("form") or "").strip().upper()
        if not form.startswith(("SC 13D", "SC 13G")):
            continue
        event = _event_from_row(row, form)
        if event.event_id not in seen:
            seen.add(event.event_id)
            events.append(event)
    for row in (rows_form4 or []):
        if not isinstance(row, dict):
            continue
        event = _event_from_row(row, "4")
        if event.event_id not in seen:
            seen.add(event.event_id)
            events.append(event)
    return events


def fetch_sec_filings_provider(
    lookback_8k_hours: int = 24,
    lookback_13dg_hours: int = 96,
) -> SourceResult:
    """适配现有 SEC provider (不重复抓取): 8-K + SC 13D/G.

    注意: 8-K 与现有 raw SEC_EDGAR_8K_ATOM 源二选一; 由
    `event_stream.sec_provider_source_enabled` 切换, 默认 false.
    """
    started = time.perf_counter()
    errors: List[str] = []
    rows_8k: List[Dict[str, Any]] = []
    rows_13dg: List[Dict[str, Any]] = []
    rows_form4: List[Dict[str, Any]] = []
    try:
        from app.data.sec_8k_provider import fetch_recent_8k

        rows_8k = fetch_recent_8k(lookback_hours=lookback_8k_hours) or []
    except Exception as exc:
        errors.append(f"8-K provider: {type(exc).__name__}: {exc}")
    try:
        from app.data.sec_8k_provider import fetch_recent_13dg

        rows_13dg = fetch_recent_13dg(lookback_hours=lookback_13dg_hours) or []
    except Exception as exc:
        errors.append(f"13D/G provider: {type(exc).__name__}: {exc}")
    try:
        from app.data.sec_form4_discovery import discover_recent_form4

        rows_form4 = discover_recent_form4(limit=20) or []
    except Exception as exc:
        errors.append(f"Form 4 discovery: {type(exc).__name__}: {exc}")

    events = build_provider_events(rows_8k, rows_13dg, rows_form4)
    latency = int((time.perf_counter() - started) * 1000)
    if errors and not events:
        return SourceResult(
            source_id=SOURCE_ID,
            source_tier=SOURCE_TIER,
            status="ERROR",
            latency_ms=latency,
            error="; ".join(errors)[:500],
        )

    payload_text = json.dumps(
        {"8k": rows_8k, "13dg": rows_13dg, "form4": rows_form4},
        ensure_ascii=False, sort_keys=True)
    payload_hash = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
    raw = RawEvent(
        raw_id=make_raw_id(SOURCE_ID, payload_hash),
        source_id=SOURCE_ID,
        source_tier=SOURCE_TIER,
        source_url=PROVIDER_SOURCE,
        source_published_at=None,
        fetched_at=datetime.now(timezone.utc).isoformat(),
        payload_hash=payload_hash,
        raw_artifact=payload_text,
        parse_status="PARSED",
    )
    return SourceResult(
        source_id=SOURCE_ID,
        source_tier=SOURCE_TIER,
        status="PARTIAL" if errors else "OK",
        latency_ms=latency,
        raw_event=raw,
        events=events,
        error="; ".join(errors)[:500] if errors else None,
    )
