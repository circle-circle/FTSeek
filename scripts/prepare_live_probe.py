#!/usr/bin/env python3
"""Prepare the LIVE lane offline without enabling real trading.

The default run is read-only. ``--apply`` first takes a WAL-safe SQLite backup,
then runs the app's real schema migrations, removes zero-size manual zombie
rows, initializes the live capital baseline from the REAL account, and leaves
all trading switches unchanged.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

LIVE_DB = ROOT / "data" / "ftseek_us_live.db"
BACKUP_DIR = ROOT / "data" / "backups"


def _backup_live_db() -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = BACKUP_DIR / f"ftseek_us_live_before_probe_prep_{stamp}.db"
    target.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(LIVE_DB, timeout=15)
    try:
        source.backup(sqlite3.connect(target))
    finally:
        source.close()
    return target


def _capital_row() -> Dict[str, Any]:
    connection = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True, timeout=5)
    try:
        row = connection.execute(
            "SELECT initial_capital, current_equity, peak_equity, water_level, "
            "is_locked, last_update FROM capital_state WHERE trade_env=0"
        ).fetchone()
        return dict(zip(
            ("initial_capital", "current_equity", "peak_equity", "water_level",
             "is_locked", "last_update"), row
        )) if row else {}
    finally:
        connection.close()


def _zombie_rows() -> int:
    connection = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True, timeout=5)
    try:
        row = connection.execute(
            "SELECT COUNT(*) FROM open_positions "
            "WHERE owner='manual' AND COALESCE(size,0)<=0"
        ).fetchone()
        return int(row[0] or 0)
    finally:
        connection.close()


def _apply_migrations() -> Dict[str, str]:
    from scripts.cutover_rehearse import apply_migrations
    results = dict(apply_migrations(LIVE_DB))
    from app.analysis.voting_accuracy_tracker import VotingAccuracyTracker
    try:
        VotingAccuracyTracker(str(LIVE_DB))
        results["voting_predictions"] = "ok"
    except Exception as error:
        results["voting_predictions"] = f"FAIL {type(error).__name__}: {error}"
    return results


def _cleanup_zombie_rows() -> int:
    connection = sqlite3.connect(LIVE_DB, timeout=15)
    try:
        cursor = connection.execute(
            "DELETE FROM open_positions "
            "WHERE owner='manual' AND COALESCE(size,0)<=0"
        )
        connection.commit()
        return int(cursor.rowcount or 0)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _initialize_capital_baseline(refresh: bool) -> Dict[str, Any]:
    from app.execution.dual_broker import get_dual_broker
    from app.risk.capital_safety import CapitalSafetySystem

    broker = get_dual_broker()
    try:
        account = broker.get_account_info("real")
    finally:
        broker.disconnect()
    equity = float(account.get("total_assets", 0) or 0) if account else 0.0
    if equity <= 0:
        raise RuntimeError("REAL account equity unavailable; refuse to fabricate a baseline")

    capital = CapitalSafetySystem(
        db_path=str(LIVE_DB), initial_capital=equity, trade_env=0
    )
    if refresh and _capital_row():
        capital.reset_capital(equity)
    capital.update_account(
        current_equity=equity,
        cash=float(account.get("cash", 0) or 0),
        market_value=float(account.get("market_val", 0) or 0),
        buying_power=float(account.get("buying_power", 0) or 0),
    )
    return {
        "total_assets": equity,
        "cash": float(account.get("cash", 0) or 0),
        "market_val": float(account.get("market_val", 0) or 0),
        "buying_power": float(account.get("buying_power", 0) or 0),
        "capital_row": _capital_row(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write LIVE preparation state")
    parser.add_argument("--offline", action="store_true", help="schema-only; do not query broker")
    parser.add_argument("--refresh-capital", action="store_true",
                        help="reset an existing live baseline before refreshing it")
    parser.add_argument("--json", default="", help="write a preparation report")
    args = parser.parse_args()

    if not LIVE_DB.exists():
        print(f"LIVE DB does not exist: {LIVE_DB}", file=sys.stderr)
        return 2

    report: Dict[str, Any] = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "apply": args.apply,
        "offline": args.offline,
        "live_db": str(LIVE_DB),
        "capital_before": _capital_row(),
        "zero_size_manual_rows_before": _zombie_rows(),
    }

    try:
        if args.apply:
            report["backup"] = str(_backup_live_db())
            report["migrations"] = _apply_migrations()
            report["zero_size_manual_rows_deleted"] = _cleanup_zombie_rows()
            if args.offline:
                report["capital"] = {"skipped": "offline"}
            else:
                report["capital"] = _initialize_capital_baseline(args.refresh_capital)
        else:
            report["migrations"] = {"skipped": "dry-run"}
            report["zero_size_manual_rows_deleted"] = 0
            report["capital"] = {"skipped": "dry-run"}

        report["capital_after"] = _capital_row()
        report["zero_size_manual_rows_after"] = _zombie_rows()
        report["ok"] = bool(report["capital_after"] or args.offline)
        if args.json:
            Path(args.json).write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ok"] else 1
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
        print(json.dumps(report, ensure_ascii=False, indent=2), file=sys.stderr)
        if args.json:
            Path(args.json).write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        return 2


if __name__ == "__main__":
    sys.exit(main())
