#!/usr/bin/env python3
"""Read-only reconciliation of broker fills, executor audit, and local books."""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(str(ROOT))
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env.us")
except Exception:
    pass

from scripts.extended_hours_order_lifecycle import query_order_rows

DB = ROOT / "data" / "ftseek_us.db"
AUDIT_LOG = ROOT / "data" / "extended_hours_exit_executions.jsonl"
REPORT = ROOT / "data" / "extended_hours_exit_reconciliation_latest.json"


def load_audit_records(path):
    records = {}
    if not Path(path).exists():
        return records
    with Path(path).open() as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except Exception:
                continue
            order_id = str(record.get("order_id") or "")
            if not order_id:
                continue
            previous = records.get(order_id)
            if previous is None or record.get("db_updated") is True:
                records[order_id] = record
    return records


def load_db_state(db_path, symbols):
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" for _ in symbols)
        positions = [
            dict(row)
            for row in conn.execute(
                f"SELECT symbol, direction, size, avg_entry_price, entry_time, owner, "
                f"trader_type, owner_strategy FROM open_positions "
                f"WHERE size > 0 AND symbol IN ({placeholders})", symbols)
        ]
        trades = [
            dict(row)
            for row in conn.execute(
                f"SELECT id, symbol, direction, entry_price, quantity, exit_time, exit_price, "
                f"exit_reason, close_futu_order_id, source_type, owner_strategy, created_at, "
                f"data_quality "
                f"FROM trade_records WHERE symbol IN ({placeholders}) ORDER BY id", symbols)
        ]
        return {"positions": positions, "trades": trades}
    finally:
        conn.close()


def _is_open_trade(trade):
    return not str(trade.get("exit_time") or "") or not float(trade.get("exit_price") or 0)


def _parse_time(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace(" ", "T"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(ZoneInfo("America/New_York")).replace(tzinfo=None)
    return parsed


def _same_qty(left, right):
    return abs(float(left or 0) - float(right or 0)) <= 1e-9


def build_reconciliation(order_rows, audit_records, db_state):
    exit_orders = [
        row for row in order_rows
        if str(row.get("remark") or "").startswith("ext_exit_")
        and float(row.get("filled_qty") or 0) > 0
        and float(row.get("avg_fill_price") or 0) > 0
    ]
    symbols = sorted({row["symbol"] for row in exit_orders})
    positions = {row["symbol"]: row for row in db_state["positions"]}
    trades = {symbol: [] for symbol in symbols}
    for trade in db_state["trades"]:
        trades.setdefault(trade["symbol"], []).append(trade)

    results = []
    mismatch_count = 0
    for order in sorted(exit_orders, key=lambda row: str(row.get("order_id"))):
        order_id = str(order.get("order_id") or "")
        audit = audit_records.get(order_id)
        symbol = order["symbol"]
        order_time = _parse_time(order.get("create_time"))
        matched_trades = [
            trade for trade in trades.get(symbol, [])
            if str(trade.get("close_futu_order_id") or "") == order_id
        ]
        open_trades = [
            trade for trade in trades.get(symbol, [])
            if _is_open_trade(trade)
            and _same_qty(trade.get("quantity"), order.get("qty"))
            and order_time is not None
            and _parse_time(trade.get("created_at")) is not None
            and _parse_time(trade.get("created_at")) <= order_time
        ]
        duplicate_candidates = [
            trade for trade in trades.get(symbol, [])
            if str(trade.get("close_futu_order_id") or "") != order_id
            and _same_qty(trade.get("quantity"), order.get("qty"))
            and order_time is not None
            and _parse_time(trade.get("created_at")) is not None
            and _parse_time(trade.get("created_at")) >= order_time
            and str(trade.get("exit_reason") or "") in {"external_close", "position_sync"}
            and str(trade.get("data_quality") or "") != "duplicate_external_close"
        ]
        audit_confirmed = bool(audit and audit.get("ok") is True and audit.get("db_updated") is True)
        db_matched = bool(matched_trades)
        related_position = positions.get(symbol)
        if related_position and not (
            open_trades and _same_qty(related_position.get("size"), order.get("filled_qty"))
        ):
            related_position = None
        issues = []
        if not audit:
            issues.append("audit_missing")
        elif not audit_confirmed:
            issues.append("audit_not_confirmed")
        if not db_matched:
            issues.append("db_close_missing")
        if related_position:
            issues.append("open_position_still_exists")
        if duplicate_candidates:
            issues.append("duplicate_close_record")
        if issues:
            mismatch_count += 1
        results.append({
            "order": order,
            "audit": audit,
            "db_close_trades": matched_trades,
            "open_trades": open_trades,
            "duplicate_close_candidates": duplicate_candidates,
            "open_position": related_position,
            "audit_confirmed": audit_confirmed,
            "db_matched": db_matched,
            "issues": issues,
        })

    return {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "summary": {
            "broker_filled_exit_orders": len(exit_orders),
            "audit_confirmed": sum(1 for result in results if result["audit_confirmed"]),
            "db_matched": sum(1 for result in results if result["db_matched"]),
            "mismatches": mismatch_count,
            "open_position_conflicts": sum(1 for result in results if result["open_position"]),
            "duplicate_close_records": sum(1 for result in results if result["duplicate_close_candidates"]),
        },
        "orders": results,
    }


def write_report(report, path):
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    temporary.replace(output)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry", action="store_true")
    parser.add_argument("--output", default=str(REPORT))
    args = parser.parse_args()

    from futu import OpenSecTradeContext, TrdMarket, SecurityFirm

    env = os.getenv("FUTU_TRD_ENV", "SIMULATE")
    account_id = int(os.getenv("SIMULATE_ACCOUNT_ID", "19930823"))
    context = OpenSecTradeContext(
        host=os.getenv("FUTU_OPD_HOST", "127.0.0.1"),
        port=int(os.getenv("FUTU_OPD_PORT", "11111")),
        filter_trdmarket=TrdMarket.US,
        security_firm=SecurityFirm.NONE,
    )
    try:
        order_rows = query_order_rows(context, env, account_id)
    finally:
        context.close()

    symbols = sorted({
        str(row.get("symbol")) for row in order_rows
        if str(row.get("remark") or "").startswith("ext_exit_")
    })
    report = build_reconciliation(
        order_rows,
        load_audit_records(AUDIT_LOG),
        load_db_state(DB, symbols) if symbols else {"positions": [], "trades": []},
    )
    if args.dry:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        write_report(report, args.output)
        print(json.dumps(report["summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()
