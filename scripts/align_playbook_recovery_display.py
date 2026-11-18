#!/usr/bin/env python3
"""审计化对齐 Playbook 恢复路径合成的 open_positions SL/TP 展示值。

默认只报告；--apply 才写入，并先落 data/audits/playbook_recovery_display_<ts>.jsonl。
严格 CAS：仅处理未平 Playbook 仓、initial_sl/current_sl 仍相等（未被 trailing 收紧）、
**SL 或 TP 至少一腿**与 trade_records 真值不一致（>0.005 绝对价）、且 take_profit_json
为空/[] 的恢复污染行；无分叉的那一腿原样保留。
（2026-11-17 DEB-20261117 P1：原先要求 SL **也**分叉，导致 RFC-195 运行时已修好 SL、
TP 仍是合成值的行永远进不了候选 — 实测 US.ELV 报 "无需对齐 (0 行)"。）
出场链读 trade_records.metadata_json，本脚本只修展示/旧风控取值面，不改交易行为。
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
SL_EPSILON = 0.005
TRAIL_EPSILON = 1e-6


def _tp_json_empty(value: object) -> bool:
    text = str(value or "").strip()
    if not text:
        return True
    try:
        parsed = json.loads(text)
    except Exception:
        return False
    return parsed is None or (isinstance(parsed, list) and len(parsed) == 0)


def find_rows(conn: sqlite3.Connection) -> List[Dict[str, object]]:
    rows = conn.execute("""
        SELECT o.symbol, o.owner_strategy, o.strategy_id,
               o.initial_sl, o.current_sl, o.initial_tp, o.take_profit_json,
               t.id AS tr_id, t.metadata_json
        FROM open_positions o
        LEFT JOIN trade_records t
          ON t.id = (SELECT id FROM trade_records t2
                     WHERE t2.symbol = o.symbol
                       AND t2.source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE')
                       AND (t2.exit_time IS NULL OR t2.exit_time='')
                     ORDER BY t2.id DESC LIMIT 1)
        WHERE o.owner_strategy IN ('playbook_sim','playbook_live')
    """).fetchall()
    candidates: List[Dict[str, object]] = []
    for row in rows:
        try:
            metadata = json.loads(row["metadata_json"]) if row["metadata_json"] else {}
        except Exception:
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        truth_sl = float(metadata.get("stop_loss") or 0)
        truth_tp = float(metadata.get("take_profit") or 0)
        old_sl = float(row["initial_sl"] or 0)
        old_current_sl = float(row["current_sl"] or 0)
        old_tp = float(row["initial_tp"] or 0)
        if truth_sl <= 0 or truth_tp <= 0 or old_sl <= 0 or old_tp <= 0:
            continue
        if abs(old_current_sl - old_sl) >= TRAIL_EPSILON:
            continue
        # [DEB-20261117 P1] 洞③ 同源修复: 原先要求 SL **也**分叉才算候选 ⇒ RFC-195 运行时
        #   对齐已把 SL 修好、TP 却仍是发现路径合成值 (cost×(1∓5%)) 的行永远进不了候选。
        #   2026-11-17 实测 US.ELV: SL 420.28 已与 trade_records 真值同源, TP 493.22 vs
        #   真值 475.73, 本脚本却报 "无需对齐 (0 行)"。改为 OR(SL 分叉, TP 分叉)。
        sl_drift = abs(old_sl - truth_sl) > SL_EPSILON
        tp_drift = abs(old_tp - truth_tp) > SL_EPSILON
        if not (sl_drift or tp_drift):
            continue
        if not _tp_json_empty(row["take_profit_json"]):
            continue
        candidates.append({
            "symbol": row["symbol"],
            "tr_id": row["tr_id"],
            "owner_strategy": row["owner_strategy"],
            "strategy_id": row["strategy_id"],
            "old_initial_sl": old_sl,
            "old_current_sl": old_current_sl,
            "old_initial_tp": old_tp,
            "old_take_profit_json": row["take_profit_json"],
            "truth_sl": truth_sl,
            "truth_tp": truth_tp,
            # 无分叉的腿原样保留, 不制造"改了但其实没改"的审计噪声
            "new_initial_sl": truth_sl if sl_drift else old_sl,
            "new_current_sl": truth_sl if sl_drift else old_current_sl,
            "new_initial_tp": truth_tp if tp_drift else old_tp,
            "new_take_profit_json": (json.dumps(
                [{"ratio": 1.0, "price": truth_tp}], ensure_ascii=False
            ) if tp_drift else row["take_profit_json"]),
            "reason": ("recovery_sl_tp_pollution" if sl_drift and tp_drift
                       else "recovery_tp_pollution" if tp_drift
                       else "recovery_sl_pollution"),
        })
    return candidates


def apply_rows(conn: sqlite3.Connection, rows: List[Dict[str, object]],
               audit_path: Path) -> int:
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        audit_path.write_text("", encoding="utf-8")
        return 0
    written = 0
    with audit_path.open("w", encoding="utf-8") as audit_file:
        for row in rows:
            from app.utils.timezone import now_et
            cursor = conn.execute(
                "UPDATE open_positions SET initial_sl=?, current_sl=?, initial_tp=?, "
                "take_profit_json=?, updated_at=? "
                "WHERE symbol=? AND ABS(initial_sl-?)<1e-6 "
                "AND ABS(current_sl-?)<1e-6 AND ABS(initial_tp-?)<1e-6 "
                "AND COALESCE(take_profit_json,'')=COALESCE(?,'')",
                (
                    row["new_initial_sl"], row["new_current_sl"], row["new_initial_tp"],
                    row["new_take_profit_json"], now_et().isoformat(),
                    row["symbol"], row["old_initial_sl"], row["old_current_sl"],
                    row["old_initial_tp"], row["old_take_profit_json"],
                ),
            )
            written += int(cursor.rowcount or 0)
            audit_row = dict(row, applied=bool(cursor.rowcount), rowcount=int(cursor.rowcount or 0))
            audit_file.write(json.dumps(audit_row, ensure_ascii=False) + "\n")
    conn.commit()
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--audit-dir", default=str(DEFAULT_AUDIT_DIR))
    parser.add_argument("--apply", action="store_true", help="实际写入 (默认只报告)")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"[align-recovery] DB 不存在: {db_path}")
        return 2
    conn = sqlite3.connect(str(db_path), timeout=60)
    conn.row_factory = sqlite3.Row
    try:
        rows = find_rows(conn)
        if not rows:
            print("[align-recovery] 无需对齐 (0 行)")
            return 0
        print(f"[align-recovery] 候选 {len(rows)} 行:")
        for row in rows:
            print(f"  {row['symbol']:12} SL {row['old_initial_sl']}→{row['new_initial_sl']} "
                  f"TP {row['old_initial_tp']}→{row['new_initial_tp']} "
                  f"reason={row['reason']}")
        if not args.apply:
            print("[align-recovery] 未写入 (加 --apply 实际执行)")
            return 0
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        audit_path = Path(args.audit_dir) / f"playbook_recovery_display_{timestamp}.jsonl"
        written = apply_rows(conn, rows, audit_path)
        print(f"[align-recovery] 已写入 {written} 行; 审计: {audit_path}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
