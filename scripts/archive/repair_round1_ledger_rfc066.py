#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RFC-066 数据修复: 重建被再入场重武装覆盖的第一轮指令台账 (真行重建版).

前提: _migrate_drop_date_symbol_unique 已跑 (UNIQUE(date,symbol) 已移除, 多行可共存).

根因回顾 (2026-08-25 实锤):
  旧 UNIQUE(date,symbol) + save_instruction UPSERT + 重武装复位 SQL →
  第一轮成交的台账 (executed/outcome/pnl/exit链) 被整轮抹除, 现存行挂着
  第二轮的 decision_id/thesis (B类: 空壳; A类: 挂第二轮结算)。

修复 (本脚本, 与 references/reentry-upsert-ledger-overwrite-20260825.md 设计一致):
  A类 (现存行挂第二轮结算, 10 行): 从现存行 metadata_json.original_instruction
    快照 + trade_records 第一轮成交重建**新的第一轮台账行** (多行共存),
    第一轮成交的 metadata_json.playbook_instruction_id 改指新行。
  B类 (现存行空壳, 15 行): 同 A 类 (重建第一轮行 + 改指链接)。
  C类 (7/10 NVDA, 1 行): 现存行本身就是第一轮结算但 decision_id 被次日生成覆盖 →
    原地把 decision_id 改回第一轮真值, 不建新行。
  审计: 每个重建/修正动作在行 metadata_json 打标记 (rfc066_backfill / rfc066_did_repair),
  幂等: 已打标的跳过。

用法: python3 scripts/repair_round1_ledger_rfc066.py [--apply]
      (无 --apply = DRY-RUN, 只报不改)
"""
import json
import sqlite3
import sys
from datetime import datetime, timezone

DB = "/opt/ftseek/FTSeek-US/data/ftseek_us.db"
DRY = "--apply" not in sys.argv

con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row

# ── 0. 前置校验: 迁移必须已完成 ──────────────────────────────────────────
_sql = con.execute(
    "SELECT sql FROM sqlite_master WHERE type='table' AND name='playbook_instructions'"
).fetchone()[0]
if "UNIQUE(date, symbol)" in _sql:
    print("ABORT: 表仍带 UNIQUE(date,symbol) — 先跑迁移 (_migrate_drop_date_symbol_unique)")
    sys.exit(1)

# ── 1. 断裂普查 (权威口径: trade 的 iid 链接行 decision_id ≠ trade.decision_id) ──
broken = con.execute("""
SELECT t.id tid, t.decision_id t_did, t.metadata_json tj,
       i.id iid, i.decision_id i_did
FROM trade_records t
JOIN playbook_instructions i
  ON i.id = CAST(json_extract(t.metadata_json,'$.playbook_instruction_id') AS INTEGER)
WHERE COALESCE(t.decision_id,'') <> '' AND i.decision_id <> t.decision_id
  AND t.exit_reason NOT LIKE '%GHOST%' AND t.exit_reason NOT LIKE '%ORPHAN%'
""").fetchall()
print(f"断裂行: {len(broken)}")

TRADE_COLS = [r[1] for r in con.execute("PRAGMA table_info(trade_records)").fetchall()]

n_new, n_repair, n_skip = 0, 0, 0
for b in broken:
    tid, iid = b["tid"], b["iid"]
    row = con.execute("SELECT * FROM playbook_instructions WHERE id=?", (iid,)).fetchone()
    try:
        meta = json.loads(row["metadata_json"] or "{}") if row["metadata_json"] else {}
    except Exception:
        meta = {}
    snap = meta.get("original_instruction") or {}

    # ── C 类: 现存行本身就是第一轮结算 (无快照), decision_id 被后来覆盖 ──
    if not snap:
        if meta.get("rfc066_did_repair"):
            n_skip += 1
            print(f"  SKIP #{iid} {row['symbol']} {row['date']} (已修复)")
            continue
        if row["executed"] != 1 or not row["outcome"]:
            n_skip += 1
            print(f"  SKIP #{iid} {row['symbol']} {row['date']} 未结算且无快照, 无法判定")
            continue
        print(f"  C类 #{iid} {row['symbol']} {row['date']}: decision_id 原地回指 "
              f"{row['decision_id']!r} → {b['t_did']!r} (结算保持不动)")
        if not DRY:
            meta["rfc066_did_repair"] = {
                "at_utc": datetime.now(timezone.utc).isoformat(),
                "old_did": row["decision_id"], "new_did": b["t_did"],
                "reason": "RFC-066: 行是第一轮结算壳, did 被后续生成覆盖, 回指成交真值",
            }
            con.execute("UPDATE playbook_instructions SET decision_id=?, metadata_json=? "
                        "WHERE id=?", (b["t_did"], json.dumps(meta, ensure_ascii=False), iid))
        n_repair += 1
        continue

    # ── A/B 类: 幂等守卫 (现存行已打过第一轮重建标记) ──
    if meta.get("rfc066_backfill_round1"):
        n_skip += 1
        print(f"  SKIP #{iid} {row['symbol']} {row['date']} (round1 已重建)")
        continue

    # 第一轮成交 (快照 decision_id + 当日精确联接)
    t = con.execute("""
        SELECT id, direction, entry_price, exit_price, entry_time, exit_time,
               quantity, realized_pnl, pnl_net, fees_est, pnl_pct, exit_reason,
               holding_hours, mfe_pct, mae_pct, market_regime
        FROM trade_records
        WHERE COALESCE(decision_id,'') = ? AND substr(entry_time,1,10) = ?
    """, (snap.get("decision_id"), row["date"])).fetchall()
    if len(t) != 1:
        n_skip += 1
        print(f"  SKIP #{iid} {row['symbol']} {row['date']}: 第一轮成交匹配数={len(t)} (非1)")
        continue
    t = t[0]
    pnl_net = t["pnl_net"] if t["pnl_net"] is not None else \
        (t["realized_pnl"] or 0.0) - (t["fees_est"] or 0.0)
    outcome = "win" if pnl_net > 0 else "loss"
    notional = float(t["entry_price"] or 0) * float(t["quantity"] or 0)
    mfe_usd = round(t["mfe_pct"] / 100.0 * notional, 2) if t["mfe_pct"] is not None else None
    mae_usd = round(t["mae_pct"] / 100.0 * notional, 2) if t["mae_pct"] is not None else None

    # ── 重建第一轮行: 以现存行为列模板 (保留生成期字段), 覆盖第一轮结算列 ──
    cols = [c for c in row.keys() if c not in ("id",)]
    vals = {c: row[c] for c in cols}
    vals.update({
        "decision_id": snap.get("decision_id"),
        "direction": snap.get("direction") or row["direction"],
        "thesis": snap.get("thesis"),
        "confidence": snap.get("confidence"),
        "entry_price": snap.get("entry_price"),
        "stop_loss": snap.get("stop_loss"),
        "take_profit": snap.get("take_profit"),
        "raw_instruction_json": snap.get("raw_instruction_json") or vals.get("raw_instruction_json"),
        # 第一轮结算 (权威: trade_records)
        "triggered": 1,
        "executed": 1,
        "execution_price": t["entry_price"],
        "execution_time": t["entry_time"],
        "exit_price": t["exit_price"],
        "exit_time": t["exit_time"],
        "exit_reason": t["exit_reason"],
        "holding_period_hours": t["holding_hours"],
        "pnl": round(pnl_net, 2),                 # 净口径 (2026-08-20 用户裁)
        "pnl_pct": t["pnl_pct"],
        "outcome": outcome,
        "max_favorable_excursion": mfe_usd,
        "max_adverse_excursion": mae_usd,
        "mfe_analysis": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    r1_meta = {
        "rfc066_backfill_round1": {
            "at_utc": datetime.now(timezone.utc).isoformat(),
            "rebuilt_from_row": iid,
            "snapshot_source": "metadata_json.original_instruction",
            "trade_id": t["id"],
            "reason": "RFC-066: 第一轮台账被再入场重武装 UPSERT 抹除, 从快照+trade_records 重建",
        },
        "source": "intraday_reentry",   # 血缘: 本行由再入场流程的覆盖事故产生
    }
    vals["metadata_json"] = json.dumps(r1_meta, ensure_ascii=False)
    # fwd_return_* 是现存行(第二轮)的模拟值, 语义上不属于第一轮 → 清空
    for c in cols:
        if c.startswith("fwd_return") or c == "shadow_pnl" or c == "lesson":
            vals[c] = None

    col_list = ", ".join(cols)
    ph = ", ".join("?" for _ in cols)
    print(f"  A/B类 #{iid} {row['symbol']} {row['date']}: 重建第一轮行 "
          f"did={snap.get('decision_id')[:40]} dir={vals['direction']} "
          f"{outcome} net={pnl_net:+.2f} (trade#{tid} 链接将改指新行)")
    if not DRY:
        cur = con.execute(
            f"INSERT INTO playbook_instructions ({col_list}) VALUES ({ph})",
            [vals[c] for c in cols])
        new_id = cur.lastrowid
        # 现存行打审计标记
        meta["rfc066_backfill_round1"] = {
            "at_utc": datetime.now(timezone.utc).isoformat(),
            "new_row_id": new_id,
            "round1_decision_id": snap.get("decision_id"),
            "trade_id": t["id"],
        }
        con.execute("UPDATE playbook_instructions SET metadata_json=? WHERE id=?",
                    (json.dumps(meta, ensure_ascii=False), iid))
        # trade 链接改指新行 (json_set 只动一个键, 其余保留)
        tj = json.loads(b["tj"] or "{}")
        tj["playbook_instruction_id"] = new_id
        tj["rfc066_repoint"] = {"from": iid, "to": new_id,
                                 "at_utc": datetime.now(timezone.utc).isoformat()}
        con.execute("UPDATE trade_records SET metadata_json=? WHERE id=?",
                    (json.dumps(tj, ensure_ascii=False), tid))
    n_new += 1

if not DRY:
    con.commit()
con.close()
print(f"\nmode={'APPLY' if not DRY else 'DRY-RUN'}  重建={n_new} 原地修正={n_repair} 跳过={n_skip}")
