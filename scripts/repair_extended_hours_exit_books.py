#!/usr/bin/env python3
"""Controlled repair of historical extended-hours exit book mismatches."""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
os.chdir(str(ROOT))
sys.path.insert(0, str(ROOT))

from scripts.reconcile_extended_hours_exits import load_audit_records

DEFAULT_REPORT = ROOT / "data" / "extended_hours_exit_reconciliation_latest.json"
DEFAULT_DB = ROOT / "data" / "ftseek_us.db"
DEFAULT_AUDIT = ROOT / "data" / "extended_hours_exit_executions.jsonl"
DEFAULT_BACKUP = ROOT / "data" / "backups" / "ftseek_us_before_extended_hours_exit_repair.db"


def _order_time_et(order):
    try:
        parsed = datetime.fromisoformat(str(order.get("create_time")).replace(" ", "T"))
        return parsed.replace(tzinfo=ZoneInfo("America/New_York")).isoformat()
    except (TypeError, ValueError):
        return None


def _pnl(trade, order):
    sign = -1 if str(trade.get("direction") or "").upper() in {"SHORT", "SELL_SHORT"} else 1
    entry = float(trade.get("entry_price") or 0)
    exit_price = float(order.get("avg_fill_price") or 0)
    quantity = min(float(trade.get("quantity") or 0), float(order.get("filled_qty") or 0))
    pnl = (exit_price - entry) * quantity * sign
    pnl_pct = (exit_price / entry - 1) * sign if entry else 0
    return round(pnl, 2), round(pnl_pct, 4)


def build_repair_plan(report):
    actions = []
    for result in report.get("orders", []):
        order = result.get("order") or {}
        issues = set(result.get("issues") or [])
        order_id = str(order.get("order_id") or "")
        symbol = str(order.get("symbol") or "")
        if not order_id or not symbol:
            continue
        if "audit_missing" in issues or "audit_not_confirmed" in issues:
            actions.append({
                "action": "append_reconciliation_audit",
                "order_id": order_id,
                "symbol": symbol,
                "order": order,
            })
        if "db_close_missing" in issues:
            open_trades = result.get("open_trades") or []
            if len(open_trades) != 1:
                actions.append({
                    "action": "unsupported_open_trade_count",
                    "order_id": order_id,
                    "symbol": symbol,
                    "count": len(open_trades),
                })
            else:
                trade = open_trades[0]
                pnl, pnl_pct = _pnl(trade, order)
                actions.append({
                    "action": "repair_open_trade",
                    "order_id": order_id,
                    "symbol": symbol,
                    "trade_id": int(trade["id"]),
                    "exit_price": float(order["avg_fill_price"]),
                    "exit_time": _order_time_et(order),
                    "exit_reason": str(order.get("remark") or "").removeprefix("ext_exit_"),
                    "realized_pnl": pnl,
                    "pnl_pct": pnl_pct,
                })
        for trade in result.get("duplicate_close_candidates") or []:
            actions.append({
                "action": "mark_duplicate_external_close",
                "order_id": order_id,
                "symbol": symbol,
                "trade_id": int(trade["id"]),
            })
        if "open_position_still_exists" in issues:
            actions.append({
                "action": "unsupported_open_position_conflict",
                "order_id": order_id,
                "symbol": symbol,
            })
    return actions


def build_duplicate_pnl_plan(db_path):
    """维护模式: 找出 data_quality 命中中性前缀但 realized_pnl/pnl_pct 仍非 NULL 的历史行.

    起因 (2026-09-18 复查 F1): 本脚本首版只打标记、未置 NULL ⇒ 3 行幻影盈亏 -258.52
    被不做过滤的净额消费方计入。对账报告已是修复后状态 (重复候选 0), 故需独立扫描自愈。
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT id, symbol, realized_pnl, pnl_pct FROM trade_records "
            "WHERE LOWER(COALESCE(data_quality,'')) LIKE 'duplicate%' "
            "AND (realized_pnl IS NOT NULL OR pnl_pct IS NOT NULL) ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    return [
        {"action": "mark_duplicate_external_close", "order_id": "", "symbol": r[1],
         "trade_id": int(r[0]), "previous_realized_pnl": r[2], "previous_pnl_pct": r[3]}
        for r in rows
    ]


def _merge_metadata(raw, value):
    try:
        metadata = json.loads(raw) if raw else {}
    except Exception:
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    metadata.update(value)
    return json.dumps(metadata, ensure_ascii=False)


def _backup(db_path, backup_path):
    source = sqlite3.connect(str(db_path))
    target_path = Path(backup_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target = sqlite3.connect(str(target_path))
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return target_path


def apply_repair_plan(db_path, audit_path, actions, report_generated_at, backup_path=None):
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    applied = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        for action in actions:
            name = action["action"]
            if name == "repair_open_trade":
                trade = conn.execute(
                    "SELECT id, metadata_json FROM trade_records WHERE id=? "
                    "AND (COALESCE(exit_price, 0)=0 OR COALESCE(exit_time, '')='')",
                    (action["trade_id"],),
                ).fetchone()
                if not trade:
                    continue
                metadata = _merge_metadata(
                    trade["metadata_json"],
                    {
                        "extended_hours_reconciliation": {
                            "order_id": action["order_id"],
                            "report_generated_at": report_generated_at,
                            "exit_time_source": "broker_order_create_time",
                        }
                    },
                )
                conn.execute(
                    "UPDATE trade_records SET exit_price=?, exit_time=?, exit_reason=?, "
                    "realized_pnl=?, pnl_pct=?, close_futu_order_id=?, metadata_json=? WHERE id=?",
                    (action["exit_price"], action["exit_time"], action["exit_reason"],
                     action["realized_pnl"], action["pnl_pct"], action["order_id"],
                     metadata, action["trade_id"]),
                )
                applied.append(action)
            elif name == "mark_duplicate_external_close":
                # 2026-09-18 第三方复查 F1: 按 daily_invariant_check 的既定约定
                #   (data_quality 命中 duplicate/outlier/invalid/void/excluded 前缀者
                #    "故意留 NULL 使净额求和自动排除"), 标记重复时必须**同时置 NULL** realized_pnl/pnl_pct;
                #   否则任何不过滤标记的净额消费方会把幻影盈亏算进去 (实测 3 行 = -258.52)。
                #   原值不丢: 写方已把 pnl/pnl_pct 存在 metadata_json.attribution 内 (已核)。
                #   守卫放宽为"未标记 或 仍带非 NULL 盈亏" ⇒ 对历史漏改行可自愈且可重复执行。
                updated = conn.execute(
                    "UPDATE trade_records SET data_quality='duplicate_external_close', "
                    "realized_pnl=NULL, pnl_pct=NULL "
                    "WHERE id=? AND (COALESCE(data_quality,'') <> 'duplicate_external_close' "
                    "OR realized_pnl IS NOT NULL OR pnl_pct IS NOT NULL)",
                    (action["trade_id"],),
                ).rowcount
                if updated:
                    applied.append(action)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    audit_actions = [action for action in actions if action["action"] == "append_reconciliation_audit"]
    existing = load_audit_records(audit_path)
    appended = []
    if audit_actions:
        audit_path = Path(audit_path)
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with audit_path.open("a") as handle:
            for action in audit_actions:
                order = action["order"]
                if str(order.get("order_id")) in existing:
                    continue
                record = {
                    "ts": datetime.utcnow().isoformat() + "Z",
                    "trade_date": str(order.get("create_time") or "")[:10],
                    "order_id": str(order.get("order_id")),
                    "symbol": str(order.get("symbol")),
                    "qty": float(order.get("qty") or 0),
                    "price": float(order.get("price") or 0),
                    "session": str(order.get("session") or "extended_hours").lower(),
                    "reason": str(order.get("remark") or "").removeprefix("ext_exit_"),
                    "ret": 0,
                    "filled_qty": float(order.get("filled_qty") or 0),
                    "avg_fill_price": float(order.get("avg_fill_price") or 0),
                    "remaining_qty": 0.0,
                    "fully_filled": True,
                    "db_updated": True,
                    "ok": True,
                    "repair_source": "historical_reconciliation",
                    "report_generated_at": report_generated_at,
                }
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                appended.append(record)
    return {"applied": applied, "audit_appended": appended, "backup": str(backup_path or "")}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--audit", default=str(DEFAULT_AUDIT))
    parser.add_argument("--backup", default=str(DEFAULT_BACKUP))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--fix-duplicate-pnl", action="store_true",
                        help="维护模式: 把已标记为重复但仍带非 NULL 盈亏的历史行置 NULL (F1 修复)")
    args = parser.parse_args()

    if args.fix_duplicate_pnl:
        actions = build_duplicate_pnl_plan(args.db)
        if not args.apply:
            print(json.dumps({"mode": "fix_duplicate_pnl", "actions": actions},
                             ensure_ascii=False, indent=2))
            return
        backup_path = _backup(args.db, args.backup)
        result = apply_repair_plan(args.db, args.audit, actions,
                                   datetime.utcnow().isoformat() + "Z", backup_path)
        result["mode"] = "fix_duplicate_pnl"
        result["backup"] = str(backup_path)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    report = json.loads(Path(args.report).read_text())
    actions = build_repair_plan(report)
    if not args.apply:
        print(json.dumps({
            "report": args.report,
            "actions": actions,
        }, ensure_ascii=False, indent=2))
        return
    backup_path = _backup(args.db, args.backup)
    result = apply_repair_plan(
        args.db, args.audit, actions, report.get("generated_at"), backup_path
    )
    result["backup"] = str(backup_path)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
