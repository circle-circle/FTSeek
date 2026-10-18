from __future__ import annotations

import html
import json
import logging
import re
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.request import Request, urlopen

from app.data.sec_form4_provider import (
    CACHE_DIR,
    SEC_ARCHIVES_URL,
    SEC_USER_AGENT,
    SECForm4Provider,
)

logger = logging.getLogger("ftseek.form4_discovery")

GETCURRENT_FORM4_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"
    "&type=4&company=&dateb=&owner=include&count=100&output=atom"
)
CACHE_FILE = CACHE_DIR / "form4_discovery_recent.json"
CACHE_TTL_SEC = 600
DEFAULT_LIMIT = 20
MIN_DOLLAR_AMOUNT = 50_000
_FILED_RE = re.compile(r"Filed:\s*(?:</b>)?\s*(\d{4}-\d{2}-\d{2})", re.I)
_ACC_RE = re.compile(r"AccNo:\s*(?:</b>)?\s*([0-9-]+)", re.I)


def _fetch_atom(url: str = GETCURRENT_FORM4_URL) -> str:
    req = Request(url, headers={"User-Agent": SEC_USER_AGENT})
    with urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_issuer_entries(atom: str) -> List[Dict[str, Any]]:
    """只保留 Form 4 的 Issuer 条目 (Subject 公司), 按 updated 倒序."""
    out = []
    for match in re.finditer(r"<entry>(.*?)</entry>", atom, re.S):
        entry = match.group(1)
        title_m = re.search(r"<title>(.*?)</title>", entry, re.S)
        title = (title_m.group(1) if title_m else "").strip()
        if not title.upper().startswith("4 ") or "(ISSUER)" not in title.upper():
            continue
        cik_m = re.search(r"\((\d{7,10})\)", title)
        comp_m = re.search(r"^4\s*-\s*(.+?)\s*\(", title, re.I)
        summary_m = re.search(r"<summary[^>]*>(.*?)</summary>", entry, re.S)
        summary = html.unescape(summary_m.group(1)) if summary_m else ""
        filed_m = _FILED_RE.search(summary)
        acc_m = _ACC_RE.search(summary)
        link_m = re.search(r'href="([^"]+)"', entry)
        updated_m = re.search(r"<updated>(.*?)</updated>", entry)
        if not cik_m or not acc_m:
            continue
        out.append({
            "company": (comp_m.group(1) if comp_m else title)[:100],
            "cik": int(cik_m.group(1)),
            "accession": acc_m.group(1),
            "filing_date": filed_m.group(1) if filed_m else "",
            "updated": (updated_m.group(1) if updated_m else "")[:30],
            "url": link_m.group(1) if link_m else "",
        })
    out.sort(key=lambda row: row.get("updated") or "", reverse=True)
    return out


def _load_cache() -> Optional[List[Dict[str, Any]]]:
    try:
        if CACHE_FILE.exists() and time.time() - CACHE_FILE.stat().st_mtime < CACHE_TTL_SEC:
            data = json.loads(CACHE_FILE.read_text())
            return data if isinstance(data, list) else None
    except Exception as exc:
        logger.warning("[form4_discovery] cache read failed: %s", exc, exc_info=True)
    return None


def _save_cache(rows: List[Dict[str, Any]]) -> None:
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(rows, ensure_ascii=False))
    except Exception as exc:
        logger.warning("[form4_discovery] cache write failed: %s", exc, exc_info=True)


def discover_recent_form4(
    limit: int = DEFAULT_LIMIT,
    *,
    provider: Optional[SECForm4Provider] = None,
) -> List[Dict[str, Any]]:
    """发现近期全市场 Form 4, 复用现有 XML 解析器, 只保留 P/S + >=$50K + 非 10b5-1.

    Returns [{ticker, company, form, updated, url, transactions, buy_count,
              sell_count, net_buy_amount}]
    """
    cached = _load_cache()
    if cached is not None:
        return cached[:limit]

    try:
        entries = parse_issuer_entries(_fetch_atom())
    except Exception as exc:
        logger.warning("[form4_discovery] atom fetch failed: %s", exc, exc_info=True)
        return []

    provider = provider or SECForm4Provider()
    rows: List[Dict[str, Any]] = []
    for entry in entries[: max(1, limit)]:
        ticker = provider.get_ticker(entry["cik"])
        if not ticker:
            continue
        accession = entry["accession"]
        cik = entry["cik"]
        filing_url = (
            f"{SEC_ARCHIVES_URL}/{cik}/{accession.replace('-', '')}/{accession}.txt"
        )
        try:
            transactions = provider._parse_form4_filing(
                filing_url, ticker, entry["company"],
                entry.get("filing_date", ""), accession,
            )
        except Exception as exc:
            logger.warning(
                "[form4_discovery] %s %s parse failed: %s",
                ticker, accession, exc, exc_info=True,
            )
            continue
        qualified = [
            asdict(txn) for txn in transactions
            if txn.transaction_code in ("P", "S")
            and (txn.dollar_amount or 0) >= MIN_DOLLAR_AMOUNT
            and not txn.is_10b51
        ]
        if not qualified:
            continue
        buy = sum(t["dollar_amount"] for t in qualified if t["transaction_code"] == "P")
        sell = sum(t["dollar_amount"] for t in qualified if t["transaction_code"] == "S")
        rows.append({
            "ticker": ticker,
            "company": entry["company"],
            "form": "4",
            "updated": entry.get("updated") or "",
            "url": entry.get("url") or filing_url,
            "transactions": qualified,
            "buy_count": sum(1 for t in qualified if t["transaction_code"] == "P"),
            "sell_count": sum(1 for t in qualified if t["transaction_code"] == "S"),
            "net_buy_amount": round(buy - sell, 2),
        })
    _save_cache(rows)
    return rows[:limit]
