#!/usr/bin/env python3
"""用已 BOOKED 的补偿退出证据修复入场/出场账本（默认仅副本模拟）。

示例：
  PYTHONPATH=. ./venv/bin/python scripts/repair_compensation_entry_ledger.py \
    --intent-id 21bd5b51a1c04eebbc5825e2ac93e1ae
  # 确认副本模拟成功后，仅对同一 intent 加 --apply。

不下单、不查询/修改券商、不重发历史通知；生产写仅限 source DB 的
trade_records/playbook_instructions，审计写 data/audits/*.jsonl。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from app.playbook import pipeline

ROOT = Path(__file__).resolve().parents[1]
EXIT_DB = ROOT / "data" / "playbook_exits.db"
AUDIT_DIR = ROOT / "data" / "audits"


def _load_intent(intent_id: str) -> dict:
    conn = sqlite3.connect(f"file:{EXIT_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT intent_id,state,owner_json,decision_json,result_json "
            "FROM exit_intents WHERE intent_id=?", (intent_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ValueError("exit intent missing")
    if row["state"] != "BOOKED":
        raise ValueError(f"exit intent is not BOOKED: {row['state']}")
    result = json.loads(row["result_json"] or "{}")
    result["owner"] = json.loads(row["owner_json"] or "{}")
    result["decision"] = json.loads(row["decision_json"] or "{}")
    result["exit_intent_id"] = intent_id
    if result["owner"].get("kind") != "compensation":
        raise ValueError("exit intent is not an entry-bookkeeping compensation")
    if not (result.get("success") and result.get("order_filled")):
        raise ValueError("compensation exit fill evidence incomplete")
    return result


def _seal_notifications():
    # 历史补账不应再向 IM 重放一条平仓通知；仅影响本脚本进程。
    import app.push as push
    hub = SimpleNamespace(push=lambda *args, **kwargs: None,
                          push_text=lambda *args, **kwargs: None)
    push.get_push_hub = lambda: hub
    push.trade_exit = lambda **kwargs: kwargs


def _book_on_db(db_path: Path, result: dict) -> dict:
    _seal_notifications()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        before = conn.execute(
            "SELECT execution_state FROM playbook_instructions WHERE id=?",
            (result["owner"]["instruction_id"],),
        ).fetchone()
        if before is None:
            raise ValueError("source instruction missing")
        if before[0] == "BOOKED":
            existing = conn.execute(
                "SELECT id,exit_reason,pnl_net FROM trade_records WHERE "
                "json_extract(metadata_json,'$.exit_intent_id')=?",
                (result["exit_intent_id"],),
            ).fetchone()
            if existing:
                from app.playbook.pipeline_parts.compensation_repair import (
                    align_existing_reprice_metadata)
                aligned = align_existing_reprice_metadata(
                    conn, result["owner"], existing[0])
                return {"status": "REPRICE_METADATA_ALIGNED" if aligned else "ALREADY_CURRENT",
                        "trade_id": existing[0], "pnl_net": existing[2]}
            raise ValueError("instruction BOOKED but compensation trade receipt missing")
        dual = SimpleNamespace(acknowledge_close=lambda intent_id, receipt: True)
        booked = pipeline._book_playbook_exit_result(dual, result, conn=conn)
        after = conn.execute(
            "SELECT execution_state,outcome FROM playbook_instructions WHERE id=?",
            (result["owner"]["instruction_id"],),
        ).fetchone()
        trade = conn.execute(
            "SELECT id,realized_pnl,fees_est,pnl_net,exit_reason FROM trade_records "
            "WHERE json_extract(metadata_json,'$.exit_intent_id')=?",
            (result["exit_intent_id"],),
        ).fetchone()
        if booked != 1 or after[0] != "BOOKED" or trade is None:
            raise ValueError("compensation repair did not produce one closed trade")
        return {"status": "REPAIRED", "trade_id": trade[0],
                "realized_pnl": trade[1], "fees_est": trade[2],
                "pnl_net": trade[3], "exit_reason": trade[4],
                "instruction_outcome": after[1]}
    finally:
        conn.close()


def _simulate(source: Path, result: dict) -> dict:
    with tempfile.TemporaryDirectory(prefix="compensation-repair-") as tmp:
        clone = Path(tmp) / "source.db"
        source_conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        clone_conn = sqlite3.connect(clone)
        try:
            source_conn.backup(clone_conn)
        finally:
            source_conn.close()
            clone_conn.close()
        simulated = json.loads(json.dumps(result))
        simulated["owner"]["db_path"] = str(clone)
        return _book_on_db(clone, simulated)


def repair(intent_id: str, apply: bool) -> dict:
    result = _load_intent(intent_id)
    source = Path(result["owner"]["db_path"]).resolve()
    expected = (ROOT / "data" / "ftseek_us.db").resolve()
    if source != expected:
        raise ValueError(f"unexpected source DB: {source}")
    simulated = _simulate(source, result)
    if not apply:
        return {"intent_id": intent_id, "mode": "dry_run_copy", **simulated}
    outcome = _book_on_db(source, result)
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    audit = AUDIT_DIR / f"compensation_entry_repair_{stamp}.jsonl"
    row = {"ts": stamp, "intent_id": intent_id, "source_db": str(source),
           "simulation": simulated, "applied": outcome}
    with audit.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return {"intent_id": intent_id, "mode": "apply", "audit": str(audit), **outcome}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--intent-id", action="append", required=True,
                        help="BOOKED compensation intent；可重复传入，apply 必须显式指定")
    parser.add_argument("--apply", action="store_true", help="模拟成功后写 source DB 并落审计")
    args = parser.parse_args()
    try:
        for intent_id in dict.fromkeys(args.intent_id):
            print(json.dumps(repair(intent_id, args.apply), ensure_ascii=False))
        return 0
    except Exception as error:
        print(json.dumps({"status": "ERROR", "error": str(error)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
