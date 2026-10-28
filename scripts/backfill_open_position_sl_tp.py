#!/usr/bin/env python3
"""回填 open_positions 的 SL/TP 与 entry_time (与出场链同源) —— 一次性工具。

背景 [DEB-20261027 A]
--------------------
`app/core/position_sync.py` 的 `_is_pb_sim` 分支 (Playbook SIM 仓位) 写
`open_positions` 时曾硬写 `initial_sl=0, current_sl=0, initial_tp=0`, 且
`entry_time=datetime.now().isoformat()` (宿主 CST、naive)。后果:

  ① 同一笔仓在 `open_positions` 里"没有止损", 而出场链实际用的
     `trade_records.metadata_json.stop_loss` 有值 ⇒ 两表两个止损
     (实测 2026-10-27: `US.UNH` open_positions.initial_sl=0 vs trade_records 392.92);
  ② `entry_time` 与 R-12 (DEB-20260823) 已裁的 ET 口径不一致 (差 12h);
  R-12 只覆盖了 `trade_store.upsert_position_simple`, 本分支当年被漏掉。

补充 [DEB-20261028 B]
--------------------
`playbook_service` 的直写路径 (`trade_store.upsert_position_simple`) 原先也没有
`initial_tp/take_profit_json` 列 ⇒ 即使 `position_sync` 分支已能从 trade_record
取 TP，已有的 Playbook 行仍会缺失 TP (实测 `US.CRM`: trade_record=262.89,
open_positions=NULL)。本工具在该写入路径修复后，一并补齐**已存在**行的 TP。

出场链 (`app/playbook/pipeline_parts/exit_monitor_flow.py:88`) 读的是
**trade_records**, 所以止损一直是活的 —— 本工具修的是**记录一致性**, 不是
"仓位无止损"的紧急止血。写路径已在 2026-10-27 修好, 本工具只收拾历史行。

注意 (不可当作不变式)
----------------------
两表的 SL **本就不要求相等**:
  · `open_positions.initial_sl` = 执行时 re-price 后的止损 (playbook_service 写入);
  · `trade_records.metadata_json.stop_loss` = 入场时快照;
  · 出场链的早期紧SL/保本/锁利还会继续移动**生效线**。
实测 US.TSM: open_positions 422.40 vs trade_records 411.60。
故本工具**只补空缺**, 绝不覆盖已有值；TP 来源是该仓未平 trade_record 的
`metadata_json.take_profit`，JSON 结构与 `Position.take_profit_levels` 一致。

用法
----
    venv/bin/python scripts/backfill_open_position_sl_tp.py            # 只报告 (默认)
    venv/bin/python scripts/backfill_open_position_sl_tp.py --apply    # 实际写入

幂等: 写完后再跑一次, 命中行数为 0 (WHERE 条件保证)。
审计: 每次写入前把该行的改前值落到 `data/audits/open_position_sl_tp_<ts>.jsonl`。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "ftseek_us.db"
DEFAULT_AUDIT_DIR = ROOT / "data" / "audits"


def _has_tz(value: str) -> bool:
    try:
        return datetime.fromisoformat(str(value)).tzinfo is not None
    except Exception:
        return False


def find_rows(conn: sqlite3.Connection) -> List[Dict[str, object]]:
    """需要修的行: Playbook 写路径造成的 SL/TP/entry_time 缺损。

    范围严格限定在 Playbook 写路径:
      ① missing_sl  —— trade_record 有止损, open_positions 记成 0;
      ② naive_entry_time —— open_positions.entry_time 无时区 (R-12 ET 口径漏网)。
      ③ missing_tp  —— trade_record 有止盈, open_positions 记成 0/NULL;
      ④ missing_tp_json —— TP 已存在但 take_profit_json 为空。
    """
    rows = conn.execute("""
        SELECT o.symbol, o.owner_strategy, o.initial_sl, o.current_sl,
               o.initial_tp, o.take_profit_json, o.entry_time,
               t.id AS tr_id, t.entry_time AS tr_entry, t.metadata_json
        FROM open_positions o
        LEFT JOIN trade_records t
          ON t.id = (SELECT id FROM trade_records t2
                     WHERE t2.symbol = o.symbol
                       AND t2.source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE')
                       AND (t2.exit_time IS NULL OR t2.exit_time='')
                     ORDER BY t2.id DESC LIMIT 1)
        WHERE o.owner_strategy IN ('playbook_sim','playbook_live')
    """).fetchall()
    out: List[Dict[str, object]] = []
    for r in rows:
        try:
            md = json.loads(r["metadata_json"]) if r["metadata_json"] else {}
        except Exception:
            md = {}
        if not isinstance(md, dict):
            md = {}
        tr_sl = float(md.get("stop_loss") or 0)
        tr_tp = float(md.get("take_profit") or 0)
        op_sl = float(r["initial_sl"] or 0)
        op_tp = float(r["initial_tp"] or 0)
        old_tp_json = r["take_profit_json"] or ""
        new_tp = op_tp if op_tp > 0 else tr_tp
        new_tp_json = old_tp_json
        if new_tp > 0 and not str(old_tp_json).strip():
            new_tp_json = json.dumps([{"ratio": 1.0, "price": new_tp}], ensure_ascii=False)
        entry_naive = bool(r["entry_time"]) and not _has_tz(r["entry_time"])
        missing_sl = tr_sl > 0 and op_sl <= 0
        missing_tp = tr_tp > 0 and op_tp <= 0
        missing_tp_json = new_tp > 0 and not str(old_tp_json).strip()
        if not (missing_sl or entry_naive or missing_tp or missing_tp_json):
            continue
        out.append({
            "symbol": r["symbol"],
            "owner_strategy": r["owner_strategy"],
            "tr_id": r["tr_id"],
            "old_initial_sl": op_sl,
            "old_current_sl": float(r["current_sl"] or 0),
            "old_initial_tp": op_tp,
            "old_take_profit_json": old_tp_json,
            "old_entry_time": r["entry_time"],
            "new_initial_sl": op_sl if op_sl > 0 else tr_sl,
            "new_current_sl": float(r["current_sl"] or 0) if float(r["current_sl"] or 0) > 0
                              else (op_sl if op_sl > 0 else tr_sl),
            "new_initial_tp": new_tp,
            "new_take_profit_json": new_tp_json,
            "new_entry_time": (r["tr_entry"] if (r["tr_entry"] and _has_tz(r["tr_entry"]))
                               else r["entry_time"]),
            "reason": ",".join([x for x, f in (
                ("missing_sl", missing_sl),
                ("naive_entry_time", entry_naive),
                ("missing_tp", missing_tp),
                ("missing_tp_json", missing_tp_json)) if f]),
        })
    return out


def apply_rows(conn: sqlite3.Connection, rows: List[Dict[str, object]],
               audit_path: Path) -> int:
    if not rows:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text("", encoding="utf-8")
        return 0
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with audit_path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            conn.execute(
                "UPDATE open_positions SET initial_sl=?, current_sl=?, initial_tp=?, "
                "take_profit_json=?, entry_time=?, updated_at=? WHERE symbol=?",
                (r["new_initial_sl"], r["new_current_sl"], r["new_initial_tp"],
                 r["new_take_profit_json"], r["new_entry_time"],
                 datetime.now(timezone.utc).isoformat(),
                 r["symbol"]))
            n += 1
    conn.commit()
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--audit-dir", default=str(DEFAULT_AUDIT_DIR))
    ap.add_argument("--apply", action="store_true", help="实际写入 (默认只报告)")
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"[backfill] DB 不存在: {db_path}")
        return 2
    conn = sqlite3.connect(str(db_path), timeout=60)
    conn.row_factory = sqlite3.Row
    try:
        rows = find_rows(conn)
        if not rows:
            print("[backfill] 无需修复 (0 行)")
            return 0
        print(f"[backfill] 待修复 {len(rows)} 行:")
        for r in rows:
            print(f"  {r['symbol']:12} reason={r['reason']:28} "
                  f"SL {r['old_initial_sl']}→{r['new_initial_sl']} "
                  f"TP {r['old_initial_tp']}→{r['new_initial_tp']} "
                  f"entry {r['old_entry_time']} → {r['new_entry_time']}")
        if not args.apply:
            print("[backfill] 未写入 (加 --apply 实际执行)")
            return 0
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        audit = Path(args.audit_dir) / f"open_position_sl_tp_{ts}.jsonl"
        n = apply_rows(conn, rows, audit)
        print(f"[backfill] 已写入 {n} 行; 审计: {audit}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
