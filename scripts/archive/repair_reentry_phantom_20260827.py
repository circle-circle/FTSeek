#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""2026-08-27 数据修复: 再入场"幻影回填"行清洗 + 指令行 exit_time 回填.

背景 (8/26 复盘检查实锤, 与 repair_round1_ledger_rfc066.py 同族):
  根因: trade_store.query_by_date SELECT 列表缺 decision_id → shadow_tracker
  P2b 轮次级联接 (2026-08-24) 读 t.get("decision_id") 恒 None → 全部成交被视为
  "无归属" → 再入场新轮行认领第一轮成交。
  实锤: #1777 (US.RIVN BUY, 01:47 触发后被单日notional上限拦截, 从未下单) 被盖上
  #1370 的 execution/exit 链 + gross pnl 21.9 + outcome=win (伪 win, 虚增胜率)。
  代码修复已并行落地 (query_by_date 补列 + shadow_tracker 时刻不变量守卫 +
  pipeline 结算写 exit_time), 本脚本只修历史数据。

动作:
  A. 幻影行 (reentry 行 executed=1 但当日 trade_records 无同 decision_id 成交):
     复位 executed=0, 清 outcome/execution/exit/pnl 链 → outcome='missed' +
     skip_reason 写明真实拒因 (若原 skip_reason 为空)。position_size 保留
     (触发期 sizing 真实写入, 非幻影产物)。
  B. exit_time 缺失回填 (pipeline 结算 UPDATE 旧版不写 exit_time):
     outcome IN (win,loss) 且 exit_time 空且 exit_price>0 → 从 decision_id
     一致的成交行回填 exit_time。
  审计: 所有改动在 metadata_json 打 phantom_repair_20260827 标记 (含前值快照)。
  幂等: 已打标跳过。

用法: python3 scripts/repair_reentry_phantom_20260827.py [--apply]
      (无 --apply = DRY-RUN, 只报不改)
"""
import json
import sqlite3
import sys
from datetime import datetime, timezone

DB = "/opt/ftseek/FTSeek-US/data/ftseek_us.db"
DRY = "--apply" not in sys.argv
MARK = "phantom_repair_20260827"

con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row


def _flagged(meta):
    try:
        return MARK in (json.loads(meta or "{}") or {})
    except Exception:
        return False


n_a = n_b = n_skip = 0

# ── A. 幻影回填行普查: reentry 行 executed=1 但无同 decision_id 成交 ──────────
rows = con.execute("""
    SELECT * FROM playbook_instructions
    WHERE model_used='intraday_reentry_v1' AND executed=1
""").fetchall()
for r in rows:
    if _flagged(r["metadata_json"]):
        n_skip += 1
        continue
    match = con.execute(
        "SELECT id FROM trade_records WHERE trade_date=? AND decision_id=? "
        "AND exit_reason NOT LIKE '%GHOST%'",
        (r["date"], r["decision_id"])).fetchone()
    if match:
        continue  # 真成交, 不动
    n_a += 1
    before = {k: r[k] for k in ("executed", "outcome", "execution_price", "execution_time",
                                "exit_price", "exit_time", "exit_reason", "pnl", "pnl_pct",
                                "holding_period_hours", "skip_reason")}
    print(f"[A] #{r['id']} {r['date']} {r['symbol']} {r['direction']} "
          f"幻影回填 → missed (前值: outcome={before['outcome']} pnl={before['pnl']} "
          f"exit={before['exit_time']!r})")
    if DRY:
        continue
    _skip = r["skip_reason"] or (
        "已触发未执行 (trigger_no_execution: 单日notional上限/护栏拦截, 从未下单; "
        "8/26 幻影回填数据修复, 详见 08-27 复盘)")
    try:
        meta = json.loads(r["metadata_json"] or "{}")
    except Exception:
        meta = {}
    meta[MARK] = {"action": "phantom_exec_reset", "before": before}
    con.execute(
        "UPDATE playbook_instructions SET executed=0, outcome='missed', "
        "execution_price=NULL, execution_time=NULL, exit_price=NULL, exit_time=NULL, "
        "exit_reason=NULL, pnl=NULL, pnl_pct=NULL, holding_period_hours=NULL, "
        "skip_reason=?, metadata_json=?, updated_at=? WHERE id=?",
        (_skip, json.dumps(meta, ensure_ascii=False),
         datetime.now(timezone.utc).isoformat(), r["id"]))

# ── B. exit_time 缺失回填 (win/loss 行结算链完整但 exit_time 空) ───────────────
for r in con.execute("""
    SELECT * FROM playbook_instructions
    WHERE outcome IN ('win','loss') AND exit_price > 0
      AND (exit_time IS NULL OR exit_time='')
      AND decision_id IS NOT NULL AND decision_id <> ''
""").fetchall():
    if _flagged(r["metadata_json"]):
        n_skip += 1
        continue
    t = con.execute(
        "SELECT exit_time FROM trade_records WHERE trade_date=? AND decision_id=? "
        "AND exit_time IS NOT NULL AND exit_time<>'' ORDER BY id LIMIT 1",
        (r["date"], r["decision_id"])).fetchone()
    if not t:
        print(f"[B] #{r['id']} {r['date']} {r['symbol']} 无可联成交 exit_time, 跳过")
        continue
    n_b += 1
    print(f"[B] #{r['id']} {r['date']} {r['symbol']} exit_time ← {t['exit_time']}")
    if DRY:
        continue
    try:
        meta = json.loads(r["metadata_json"] or "{}")
    except Exception:
        meta = {}
    meta[MARK] = {"action": "exit_time_backfill", "value": t["exit_time"]}
    con.execute(
        "UPDATE playbook_instructions SET exit_time=?, metadata_json=?, updated_at=? WHERE id=?",
        (t["exit_time"], json.dumps(meta, ensure_ascii=False),
         datetime.now(timezone.utc).isoformat(), r["id"]))

con.commit() if not DRY else con.rollback()
print(f"{'DRY-RUN' if DRY else 'APPLIED'}: 幻影复位 {n_a}, exit_time回填 {n_b}, 已打标跳过 {n_skip}")
