#!/usr/bin/env python3
"""为 LIVE 臂已开仓但缺 broker STOP 的持仓补挂止损单 (一次性运维工具).

立项: 2026-10-06 首笔 LIVE 探针 (US.PBR 1股) 成交后 STOP 挂单被拒
(aux_price 缺失, 修复见 commit 9014d3ca1), fail-close 按设计落 HALT 但
**既有仓位不平仓** (RFC-132) ⇒ 需人工补挂并把回执落台账。

幂等: 只处理 open_positions 中 account_type=live + owner_strategy=playbook_live
+ size>0 + sl_broker_order_id 为空的行; 挂单前先查券商在途委托, 已有同标的
STOP 则只补台账不重挂。全程不动 HALT 文件 (清除是人工确认后的独立动作)。

用法:
    PYTHONPATH=. ./venv/bin/python scripts/rehang_live_stop.py --dry-run
    PYTHONPATH=. ./venv/bin/python scripts/rehang_live_stop.py --apply
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _find_missing_stop_positions(ledger: Path):
    conn = sqlite3.connect(f"file:{ledger}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("""
            SELECT symbol, direction, size, current_sl, entry_broker_order_id
            FROM open_positions
            WHERE account_type='live' AND owner_strategy='playbook_live'
              AND owner='auto' AND COALESCE(size,0) > 0
              AND COALESCE(sl_broker_order_id,'') = ''
        """).fetchall()]
    finally:
        conn.close()


def _broker_open_stop_ids(ctx, symbol: str, direction_held: str, qty: float) -> list:
    """查券商侧属于本策略的 STOP 委托 (防重复补挂/误绑手动单)."""
    from futu import OrderType as FutuOrderType, TrdEnv, TrdSide
    ret, data = ctx.order_list_query(code=symbol, trd_env=TrdEnv.REAL, refresh_cache=True)
    if ret != 0 or data is None or data.empty:
        return []
    ids = []
    expected_side = str(TrdSide.SELL if direction_held == "LONG" else TrdSide.BUY)
    expected_remark = f"pb_stop_{symbol}_real"
    for _, row in data.iterrows():
        if str(row.get("order_type", "")) != str(FutuOrderType.STOP):
            continue
        if str(row.get("code", "")) != symbol:
            continue
        if str(row.get("remark", "")) != expected_remark:
            continue
        if str(row.get("trd_side", "")) != expected_side:
            continue
        try:
            row_qty = float(row.get("qty", 0) or 0)
        except (TypeError, ValueError):
            continue
        if row_qty != float(qty):
            continue
        if str(row.get("order_status", "")).upper() in {
            "CANCELLED_ALL", "CANCELLED_PART", "FAILED", "DISABLED",
        }:
            continue
        ids.append(str(row.get("order_id", "")))
    return ids


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="只检查本地缺 STOP 持仓 (默认行为, 显式别名)")
    parser.add_argument("--apply", action="store_true", help="实际挂单 (默认 dry-run)")
    args = parser.parse_args()
    if args.dry_run and args.apply:
        parser.error("--dry-run 与 --apply 互斥")

    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env.us")  # override=False: 不覆盖进程已有环境

    from app.core.paths import get_trade_records_db_for_arm

    ledger = get_trade_records_db_for_arm("real")
    rows = _find_missing_stop_positions(ledger)
    report = {"ledger": str(ledger), "apply": args.apply, "positions": []}
    if not rows:
        report["note"] = "无缺失 STOP 的 LIVE 持仓"
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if not args.apply:
        for row in rows:
            row["result"] = "DRY-RUN"
            report["positions"].append(row)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    # 以下才需要券商连接 (futu SDK 线程退出挂死是仓内已知病灶, 见
    # tests/conftest.py _futu_thread_exit_bailout ⇒ 本脚本结束用 os._exit)
    from app.execution.dual_broker import get_dual_broker
    from app.playbook.db import route_playbook_db
    from app.playbook.playbook_service import PlaybookService

    broker = get_dual_broker()
    service = PlaybookService(route_playbook_db("real"))
    for row in rows:
        symbol = row["symbol"]
        entry = {
            "symbol": symbol,
            "direction": row["direction"],
            "size": row["size"],
            "stop_price": row["current_sl"],
            "entry_broker_order_id": row["entry_broker_order_id"],
        }
        if not row["current_sl"] or float(row["current_sl"]) <= 0:
            entry["result"] = "SKIP: current_sl 缺失"
            report["positions"].append(entry)
            continue
        if not args.apply:
            entry["result"] = "DRY-RUN"
            report["positions"].append(entry)
            continue

        ctx, acc_id, trd_env, lock = broker._ctx_and_acc("real")
        existing = _broker_open_stop_ids(
            ctx, symbol, str(row["direction"]), float(row["size"])
        )
        if existing:
            entry["result"] = f"券商已有在途 STOP {existing}, 仅补台账"
            stop_id = existing[0]
        else:
            res = broker.place_protective_stop(
                symbol, float(row["size"]), row["direction"],
                float(row["current_sl"]), target_env="real",
            )
            if not res.get("success") or not res.get("futu_order_id"):
                entry["result"] = f"FAIL: {res}"
                report["positions"].append(entry)
                continue
            stop_id = res["futu_order_id"]
            entry["result"] = f"PLACED: {stop_id}"

        # 回执落台账 (与入场时 _record_live_stop_receipt 同一实现, 不另造)
        service._record_live_stop_receipt(
            symbol, row["entry_broker_order_id"], stop_id,
            float(row["current_sl"]), int(float(row["size"])),
        )
        entry["ledger"] = "receipt persisted"
        report["positions"].append(entry)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    rc = 0 if all("FAIL" not in p.get("result", "") for p in report["positions"]) else 1
    sys.stdout.flush()
    import os
    os._exit(rc)  # futu SDK 线程不随正常退出收尾 (仓内已知), 结果已落盘/打印


if __name__ == "__main__":
    sys.exit(main())
