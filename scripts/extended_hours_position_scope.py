#!/usr/bin/env python3
"""Positive-evidence position scope for extended-hours exits."""
import sqlite3
from pathlib import Path


AUTO_TRADE_SOURCE_TYPES = {"PLAYBOOK_SIM", "PLAYBOOK_LIVE"}
MANUAL_OWNER_VALUES = {"manual", "user", "human"}


def _text(value):
    return str(value or "").strip().lower()


def _latest_open_trade(conn, symbol):
    return conn.execute(
        "SELECT id, source_type, owner_strategy, metadata_json FROM trade_records "
        "WHERE symbol=? AND (COALESCE(exit_time, '')='' OR COALESCE(exit_price, 0)=0) "
        "ORDER BY id DESC LIMIT 1",
        (symbol,),
    ).fetchone()


def _has_non_manual_evidence(position, trade):
    owner = _text(position["owner"])
    trader_type = _text(position["trader_type"])
    owner_strategy = _text(position["owner_strategy"])
    trade_source_type = _text(trade["source_type"] if trade else "")
    trade_owner_strategy = _text(trade["owner_strategy"] if trade else "")

    if owner in MANUAL_OWNER_VALUES or trader_type == "manual":
        return False
    if trade_source_type in AUTO_TRADE_SOURCE_TYPES:
        return True
    if trader_type == "auto" and owner_strategy and owner_strategy not in MANUAL_OWNER_VALUES:
        return True
    return (
        trader_type == "auto"
        and bool(trade_owner_strategy)
        and trade_owner_strategy not in MANUAL_OWNER_VALUES
    )


def load_non_manual_positions(db_path):
    from app.core.trade_identity import is_test_symbol

    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT symbol, direction, size, avg_entry_price, "
            "COALESCE(current_sl, 0), entry_time, signal_id, "
            "owner, trader_type, owner_strategy, source "
            "FROM open_positions WHERE size > 0"
        ).fetchall()
        scoped = []
        skipped_test = []
        for row in rows:
            # 2026-09-18 (DEB-20260918 §23.5): 占位标的 (US.TEST/*.TEST) 不得进入延长时段
            #   退出链 —— 否则影子每分钟为它取行情/算信号/打日志(实测 AH 影子对 US.TEST
            #   连续输出观测行), 而券商侧根本没有这个持仓。
            if is_test_symbol(row["symbol"]):
                skipped_test.append(str(row["symbol"]))
                continue
            trade = _latest_open_trade(conn, row["symbol"])
            if _has_non_manual_evidence(row, trade):
                scoped.append(tuple(row[:7]) + (
                    trade["metadata_json"] if trade else None,
                    trade["id"] if trade else None,
                    trade["source_type"] if trade else None,
                ))
        if skipped_test:
            print(f"[position_scope] 忽略占位标的持仓: {', '.join(sorted(set(skipped_test)))}")
        return scoped
    finally:
        conn.close()
