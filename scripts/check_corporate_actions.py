#!/usr/bin/env python3
"""Premarket read-only corporate-action check via Futu OpenQuoteContext.get_rehab.

Examples:
    PYTHONPATH=. ./venv/bin/python scripts/check_corporate_actions.py --from-positions
    PYTHONPATH=. ./venv/bin/python scripts/check_corporate_actions.py US.MU US.TSM

This script never connects to the trade context and never writes a ledger.  It
filters get_rehab rows to a small window around today so the output is actionable
instead of a multi-decade adjustment history dump.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCHEMA_VERSION = "futu_corporate_actions.v1"
_EVENT_FIELDS = (
    ("split_base", "split"), ("join_base", "reverse_split"),
    ("per_cash_div", "cash_dividend"), ("special_dividend", "special_dividend"),
    ("bonus_base", "bonus"), ("transfer_base", "transfer"),
    ("allot_base", "rights_issue"), ("add_base", "additional_shares"),
    ("stk_spo_ratio", "stock_offering"), ("spin_off_base", "spin_off"),
)


def _today_et() -> date:
    """Premarket windows follow the US trading calendar, not the host clock."""
    from app.utils.timezone import now_et

    return now_et().date()


def _clean(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return str(value)
    if not math.isfinite(number):
        return None
    return int(number) if number.is_integer() else number


def events_from_row(row: Mapping[str, Any]) -> list[dict]:
    """Convert one get_rehab row into explicit event kinds."""
    events = []
    for field, kind in _EVENT_FIELDS:
        value = _clean(row.get(field))
        if value not in (None, "", 0):
            events.append({"kind": kind, "field": field, "value": value})
    if not events and _clean(row.get("per_share_div_ratio")) not in (None, "", 0):
        events.append({
            "kind": "dividend_ratio", "field": "per_share_div_ratio",
            "value": _clean(row.get("per_share_div_ratio")),
        })
    return events


def _row_payload(row: Mapping[str, Any], events: list[dict]) -> dict:
    payload = {
        "ex_date": str(row.get("ex_div_date", "") or ""),
        "events": events,
    }
    for key in (
        "split_base", "split_ert", "join_base", "join_ert", "split_ratio",
        "per_cash_div", "special_dividend", "bonus_base", "bonus_ert",
        "per_share_div_ratio", "transfer_base", "transfer_ert",
        "per_share_trans_ratio", "allot_base", "allot_ert", "allotment_ratio",
        "allotment_price", "add_base", "add_ert", "stk_spo_ratio",
        "stk_spo_price", "spin_off_base", "spin_off_ert", "spin_off_ratio",
        "forward_adj_factorA", "forward_adj_factorB",
        "backward_adj_factorA", "backward_adj_factorB",
    ):
        value = _clean(row.get(key))
        if value is not None:
            payload[key] = value
    return payload


def fetch_corporate_actions(ctx: Any, symbols: Iterable[str], *,
                            start_date: date, end_date: date) -> dict:
    """Fetch and filter get_rehab evidence for each symbol."""
    from futu import RET_OK

    result = {
        "schema_version": SCHEMA_VERSION,
        "read_only": True,
        "window": {"start": start_date.isoformat(), "end": end_date.isoformat()},
        "symbols": {},
        "errors": {},
    }
    for raw_symbol in symbols:
        symbol = str(raw_symbol or "").strip().upper()
        if not symbol:
            continue
        try:
            ret, data = ctx.get_rehab(code=symbol)
            if ret != RET_OK or data is None:
                result["errors"][symbol] = str(data)
                continue
            events = []
            for row in ([] if getattr(data, "empty", True) else data.to_dict("records")):
                try:
                    ex_date = date.fromisoformat(str(row.get("ex_div_date", ""))[:10])
                except (TypeError, ValueError):
                    continue
                if not (start_date <= ex_date <= end_date):
                    continue
                row_events = events_from_row(row)
                if not row_events:
                    continue
                events.append(_row_payload(row, row_events))
            events.sort(key=lambda item: item["ex_date"])
            result["symbols"][symbol] = events
        except Exception as error:
            result["errors"][symbol] = f"{type(error).__name__}: {error}"
    return result


def _position_symbols() -> list[str]:
    """Read open symbols from both arms; failures are explicit, not silently empty."""
    from app.core.paths import get_trade_records_db_for_arm

    symbols = set()
    for arm in ("real", "sim"):
        path = Path(get_trade_records_db_for_arm(arm))
        if not path.exists():
            continue
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            rows = connection.execute(
                "SELECT DISTINCT symbol FROM trade_records "
                "WHERE (exit_time IS NULL OR exit_time='') AND symbol IS NOT NULL"
            ).fetchall()
            symbols.update(str(row[0]).strip().upper() for row in rows if row[0])
        finally:
            connection.close()
    return sorted(symbol for symbol in symbols if symbol.startswith("US."))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("symbols", nargs="*", help="US.XXX symbols (default: open positions)")
    parser.add_argument("--from-positions", action="store_true",
                        help="Read open symbols from LIVE + SIM ledgers (default when no symbols)")
    parser.add_argument("--days-before", type=int, default=7)
    parser.add_argument("--days-after", type=int, default=14)
    args = parser.parse_args(argv)

    symbols = list(args.symbols)
    if not symbols:
        symbols = _position_symbols()
    symbols = sorted({str(symbol).strip().upper() for symbol in symbols if symbol})
    if not symbols:
        print(json.dumps({
            "schema_version": SCHEMA_VERSION, "read_only": True,
            "symbols": {}, "errors": {}, "note": "无待检查标的",
        }, ensure_ascii=False, indent=2))
        return 0

    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env.us")
    from futu import OpenQuoteContext

    ctx = OpenQuoteContext(
        host=os.getenv("FUTU_OPEND_HOST", "127.0.0.1"),
        port=int(os.getenv("FUTU_OPEND_PORT", "11111")),
    )
    try:
        today = _today_et()
        result = fetch_corporate_actions(
            ctx, symbols,
            start_date=today - timedelta(days=max(0, args.days_before)),
            end_date=today + timedelta(days=max(0, args.days_after)),
        )
    finally:
        ctx.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
