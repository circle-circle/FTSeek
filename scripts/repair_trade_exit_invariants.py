#!/usr/bin/env python3
"""Controlled repair for recent trade exit invariant violations."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
# 2026-09-18 SSOT: 极值口径只有一份实现 (app/core/trade_extremes), 禁本地副本 ——
# 原副本对"极值字段不可解析"的处理与管线相反 (此处跳过/那边覆盖), 会静默分叉口径。
from app.core.trade_extremes import include_observed_prices  # noqa: E402

DB = REPO / "data" / "ftseek_us.db"
WINDOW_START = "2026-09-11"
EXPECTED_DUPLICATES = 0
EXPECTED_EXTREMES = 0


def _load_metadata(raw):
    try:
        metadata = json.loads(raw or "{}")
    except (TypeError, ValueError):
        metadata = {}
    return metadata if isinstance(metadata, dict) else {}


def _is_neutral_quality(value):
    normalized = str(value or "").strip().lower()
    return normalized.startswith(("duplicate", "outlier", "invalid", "void", "excluded"))


def repair_duplicate_cleanup_rows(conn: sqlite3.Connection) -> list[int]:
    rows = conn.execute(
        """
        SELECT a.id, b.id
        FROM trade_records a
        JOIN trade_records b
          ON b.symbol=a.symbol AND b.direction=a.direction
         AND ABS(b.entry_price-a.entry_price)<0.01
         AND ABS(b.exit_price-a.exit_price)<0.01
         AND b.quantity=a.quantity AND b.exit_time=a.exit_time
         AND b.id>a.id
        WHERE UPPER(COALESCE(a.exit_reason,''))='P3_DUP_CLEANUP'
          AND LOWER(COALESCE(a.data_quality,''))<>'duplicate_external_close'
        ORDER BY a.id
        """
    ).fetchall()

    repaired = []
    for duplicate_id, primary_id in rows:
        metadata = _load_metadata(
            conn.execute("SELECT metadata_json FROM trade_records WHERE id=?", (duplicate_id,)
                         ).fetchone()[0]
        )
        metadata["duplicate_cleanup"] = {
            "primary_trade_id": primary_id,
            "repaired_at": datetime.now().isoformat(timespec="seconds"),
        }
        conn.execute(
            """
            UPDATE trade_records
            SET data_quality='duplicate_external_close', realized_pnl=NULL, pnl_pct=NULL,
                fees_est=NULL, pnl_net=NULL, fees_source=NULL, mfe_pct=NULL, mae_pct=NULL,
                metadata_json=?
            WHERE id=?
            """,
            (json.dumps(metadata, ensure_ascii=False), duplicate_id),
        )
        repaired.append(duplicate_id)
    return repaired


def repair_exit_extremes(conn: sqlite3.Connection, window_start: str = WINDOW_START) -> list[int]:
    rows = conn.execute(
        """
        SELECT id, direction, entry_price, exit_price, metadata_json, data_quality
        FROM trade_records
        WHERE source_type LIKE 'PLAYBOOK%' AND exit_time IS NOT NULL AND exit_price>0
          AND entry_price>0 AND trade_date>=?
        ORDER BY id
        """,
        (window_start,),
    ).fetchall()

    repaired = []
    for row_id, direction, entry_price, exit_price, raw_metadata, quality in rows:
        if _is_neutral_quality(quality):
            continue
        metadata = _load_metadata(raw_metadata)
        original_peak = metadata.get("peak_price")
        original_peak_bar = metadata.get("peak_price_bar")
        original_trough = metadata.get("trough_price")
        original_trough_bar = metadata.get("trough_price_bar")
        include_observed_prices(metadata, direction, entry_price, exit_price)
        try:
            entry = float(entry_price)
            exit_value = float(exit_price)
            peak = float(metadata.get("peak_price", 0) or 0)
            trough = float(metadata.get("trough_price", 0) or 0)
        except (TypeError, ValueError):
            continue
        if entry <= 0 or peak <= 0 or trough <= 0:
            continue

        short = str(direction).upper() in {"SHORT", "SELL_SHORT"}
        sign = -1 if short else 1
        realized_pct = (exit_value - entry) * sign / entry * 100
        mfe = max(
            float(metadata.get("mfe_pct", 0) or 0),
            (peak - entry) * sign / entry * 100,
            realized_pct,
            0.0,
        )
        mae = max(
            float(metadata.get("mae_pct", 0) or 0),
            (entry - trough) * sign / entry * 100,
            -realized_pct,
            0.0,
        )
        mfe = round(min(mfe, 50.0), 4)
        mae = round(min(mae, 50.0), 4)
        metadata["mfe_pct"] = mfe
        metadata["mae_pct"] = mae
        max_drawdown = round(100.0 * (mfe + mae) / (100.0 + mfe), 4)

        current_mfe, current_mae = conn.execute(
            "SELECT mfe_pct, mae_pct FROM trade_records WHERE id=?", (row_id,)
        ).fetchone()
        if short:
            physical_invalid = (
                peak > min(entry, exit_value) * 1.005
                or float(original_peak_bar or 0) > min(entry, exit_value) * 1.005
            )
        else:
            physical_invalid = (
                peak < max(entry, exit_value) * 0.995
                or (original_peak_bar is not None
                    and float(original_peak_bar) < max(entry, exit_value) * 0.995)
            )
        original_peak_value = float(original_peak or 0)
        original_trough_value = float(original_trough or 0)
        original_calc_mfe = max(
            (original_peak_value - entry) * sign / entry * 100
            if original_peak_value > 0 else 0.0, 0.0,
        )
        original_calc_mae = max(
            (entry - original_trough_value) * sign / entry * 100
            if original_trough_value > 0 else 0.0, 0.0,
        )
        mfe_mismatch = abs(current_mfe - original_calc_mfe) > 0.05
        mae_mismatch = abs(current_mae - original_calc_mae) > 0.05
        missing_extremes = original_peak is None or original_trough is None
        changed = mfe_mismatch or mae_mismatch or physical_invalid or missing_extremes
        if not changed:
            continue
        conn.execute(
            "UPDATE trade_records SET metadata_json=?, mfe_pct=?, mae_pct=?, max_drawdown=? WHERE id=?",
            (json.dumps(metadata, ensure_ascii=False), mfe, mae, max_drawdown, row_id),
        )
        repaired.append(row_id)
    return repaired


def _backup(db_path: Path) -> Path:
    backup_path = db_path.parent / "backups" / (
        f"{db_path.stem}_before_exit_invariant_fix_{datetime.now():%Y%m%d_%H%M%S}.db"
    )
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(str(db_path))
    target = sqlite3.connect(str(backup_path))
    with target:
        source.backup(target)
    target.close()
    source.close()
    return backup_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-duplicates", type=int, default=EXPECTED_DUPLICATES)
    parser.add_argument("--expected-extremes", type=int, default=EXPECTED_EXTREMES)
    args = parser.parse_args()

    if args.apply:
        conn = sqlite3.connect(str(DB))
    else:
        source = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        conn = sqlite3.connect(":memory:")
        source.backup(conn)
        source.close()
    try:
        if args.apply:
            backup_path = _backup(DB)
            conn.execute("BEGIN IMMEDIATE")
            duplicates = repair_duplicate_cleanup_rows(conn)
            extremes = repair_exit_extremes(conn)
            conn.commit()
        else:
            backup_path = None
            duplicates = repair_duplicate_cleanup_rows(conn)
            extremes = repair_exit_extremes(conn)
            conn.rollback()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()

    counts_match = (
        len(duplicates) == args.expected_duplicates
        and len(extremes) == args.expected_extremes
    )
    print(json.dumps({
        "applied": args.apply,
        "backup": str(backup_path) if backup_path else None,
        "duplicate_trade_ids": duplicates,
        "exit_extreme_trade_ids": extremes,
        "expected": {"duplicates": args.expected_duplicates, "extremes": args.expected_extremes},
        "counts_match": counts_match,
    }, ensure_ascii=False, indent=2))
    return 0 if counts_match else 2


if __name__ == "__main__":
    sys.exit(main())
