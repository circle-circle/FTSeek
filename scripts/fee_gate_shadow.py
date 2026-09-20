#!/opt/ftseek/FTSeek-US/venv/bin/python3
"""T3 fee-path-C shadow gate collector.

Reads newly closed PLAYBOOK trades, records their real net result and the
counterfactual decision of the candidate price/notional gates, and refreshes
a forward summary. The script is read-only toward trade execution and does not
modify orders, positions, configuration, or strategy parameters.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "ftseek_us.db"
OUTPUT_PATH = ROOT / "data" / "fee_gate_shadow.jsonl"
SUMMARY_PATH = ROOT / "data" / "fee_gate_shadow_latest.json"
START_DATE = "2026-09-20"
PLAYBOOK_SOURCE_TYPES = (
    "PLAYBOOK_SIM",
    "PLAYBOOK_LIVE",
    "PLAYBOOK_SHADOW",
    "MANUAL_PLAYBOOK",
)
CANDIDATE_GATES = (
    "price_lt_1",
    "notional_lt_4000",
    "notional_lt_5000",
    "combo_price_1_notional_4000",
)

logger = logging.getLogger("ftseek.fee_gate_shadow")


def gate_decisions(entry_price: float, notional: float) -> dict[str, bool]:
    return {
        "price_lt_1": entry_price < 1.0,
        "notional_lt_4000": notional < 4000.0,
        "notional_lt_5000": notional < 5000.0,
        "combo_price_1_notional_4000": entry_price < 1.0 or notional < 4000.0,
    }


def build_shadow_row(row: dict, collected_at: str) -> dict:
    entry_price = float(row["entry_price"])
    quantity = int(row["quantity"])
    notional = entry_price * quantity
    fees_est = float(row["fees_est"])
    pnl_net = float(row["pnl_net"])
    return {
        "schema_version": 1,
        "trade_id": int(row["trade_id"]),
        "trade_date": row["trade_date"],
        "symbol": row["symbol"],
        "source_type": row["source_type"],
        "direction": row["direction"],
        "entry_time": row["entry_time"],
        "exit_time": row["exit_time"],
        "exit_reason": row["exit_reason"],
        "entry_price": round(entry_price, 6),
        "quantity": quantity,
        "notional": round(notional, 2),
        "realized_pnl": round(float(row["realized_pnl"]), 2),
        "fees_est": round(fees_est, 2),
        "pnl_net": round(pnl_net, 2),
        "fee_bps": round(fees_est / notional * 10000.0, 4),
        "pnl_caliber": "realized_pnl - fees_est",
        "would_block": gate_decisions(entry_price, notional),
        "collected_at": collected_at,
    }


def fetch_closed_rows(
    db_path: Path = DB_PATH, since: str = START_DATE
) -> list[dict]:
    placeholders = ",".join("?" for _ in PLAYBOOK_SOURCE_TYPES)
    uri = db_path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=15)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            f"""
            SELECT id AS trade_id, trade_date, symbol, source_type, direction,
                   entry_time, exit_time, exit_reason, entry_price, quantity,
                   realized_pnl, fees_est, pnl_net
            FROM trade_records
            WHERE source_type IN ({placeholders})
              AND trade_date >= ?
              AND trade_mode != 'live'
              AND exit_time IS NOT NULL AND exit_time != ''
              AND exit_reason IS NOT NULL AND exit_reason != ''
              AND exit_reason != 'P3_DUP_CLEANUP'
              AND entry_price IS NOT NULL AND entry_price > 0
              AND quantity IS NOT NULL AND quantity > 0
              AND fees_est IS NOT NULL
              AND pnl_net IS NOT NULL
            ORDER BY trade_date, exit_time, id
            """,
            (*PLAYBOOK_SOURCE_TYPES, since),
        ).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in rows]


def load_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("Skipping malformed fee gate shadow row")
    return rows


def existing_trade_ids(path: Path) -> set[int]:
    return {
        int(row["trade_id"])
        for row in load_rows(path)
        if row.get("trade_id") is not None
    }


def append_new_rows(rows: list[dict], path: Path) -> int:
    seen = existing_trade_ids(path)
    new_rows = [row for row in rows if int(row["trade_id"]) not in seen]
    if not new_rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in new_rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return len(new_rows)


def _arm_stats(rows: list[dict]) -> dict:
    if not rows:
        return {
            "n": 0,
            "pnl_sum": 0.0,
            "exp_per_trade": None,
            "win_rate": None,
            "fee_bps": None,
        }
    pnl_sum = sum(float(row["pnl_net"]) for row in rows)
    notional_sum = sum(float(row["notional"]) for row in rows)
    fees_sum = sum(float(row["fees_est"]) for row in rows)
    wins = sum(float(row["pnl_net"]) > 0 for row in rows)
    return {
        "n": len(rows),
        "pnl_sum": round(pnl_sum, 2),
        "exp_per_trade": round(pnl_sum / len(rows), 4),
        "win_rate": round(wins / len(rows), 4),
        "fee_bps": round(fees_sum / notional_sum * 10000.0, 4)
        if notional_sum > 0
        else None,
    }


def summarize(rows: list[dict], updated_at: str, excluded_rows: int = 0) -> dict:
    total_n = len(rows)
    gates = {}
    for gate in CANDIDATE_GATES:
        blocked = [row for row in rows if row["would_block"][gate]]
        kept = [row for row in rows if not row["would_block"][gate]]
        gates[gate] = {
            "blocked": _arm_stats(blocked),
            "kept": _arm_stats(kept),
            "n_loss_pct": round(len(blocked) / total_n * 100.0, 2)
            if total_n
            else None,
        }
    return {
        "schema_version": 1,
        "updated_at": updated_at,
        "start_date": START_DATE,
        "n_total": total_n,
        "excluded_rows": excluded_rows,
        "baseline": _arm_stats(rows),
        "gates": gates,
        "decision_boundary": "All values are net PnL after fees; data is observational.",
    }


def write_summary(summary: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def count_excluded_rows(db_path: Path = DB_PATH, since: str = START_DATE) -> int:
    """Count closed universe rows silently dropped by data-quality filters.

    Diagnostics only (R-13): rows with NULL/invalid price, quantity, fees,
    pnl_net, or NULL trade_mode never reach the collector; surfacing the count
    prevents quiet undercount if a writer regresses.
    """
    placeholders = ",".join("?" for _ in PLAYBOOK_SOURCE_TYPES)
    uri = db_path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=15)
    try:
        row = connection.execute(
            f"""
            SELECT COUNT(*)
            FROM trade_records
            WHERE source_type IN ({placeholders})
              AND trade_date >= ?
              AND (trade_mode IS NULL OR trade_mode != 'live')
              AND exit_time IS NOT NULL AND exit_time != ''
              AND exit_reason IS NOT NULL AND exit_reason != ''
              AND exit_reason != 'P3_DUP_CLEANUP'
              AND (trade_mode IS NULL
                   OR entry_price IS NULL OR entry_price <= 0
                   OR quantity IS NULL OR quantity <= 0
                   OR fees_est IS NULL
                   OR pnl_net IS NULL)
            """,
            (*PLAYBOOK_SOURCE_TYPES, since),
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        connection.close()


def run(
    db_path: Path = DB_PATH,
    output_path: Path = OUTPUT_PATH,
    summary_path: Path = SUMMARY_PATH,
    since: str = START_DATE,
) -> int:
    collected_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.touch(exist_ok=True)
    raw_rows = fetch_closed_rows(db_path=db_path, since=since)
    excluded = count_excluded_rows(db_path=db_path, since=since)
    if excluded:
        logger.warning(
            "[fee_gate_shadow] %d closed row(s) excluded by data-quality "
            "filters (NULL/invalid price, qty, fees, pnl_net or trade_mode)",
            excluded,
        )
    shadow_rows = [build_shadow_row(row, collected_at) for row in raw_rows]
    appended = append_new_rows(shadow_rows, output_path)
    all_rows = load_rows(output_path)
    summary = summarize(all_rows, collected_at, excluded_rows=excluded)
    write_summary(summary, summary_path)
    logger.info(
        "[fee_gate_shadow] total=%d appended=%d excluded=%d output=%s",
        summary["n_total"],
        appended,
        excluded,
        output_path.name,
    )
    return appended


def main() -> int:
    run()
    return 0


if __name__ == "__main__":
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        )
    sys.exit(main())
