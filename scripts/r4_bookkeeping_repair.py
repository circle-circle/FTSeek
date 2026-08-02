#!/usr/bin/env python3
"""R4 账面修复 (2026-08-02, 用户令"按建议执行") — 纯记账修正, 零市场动作。

修复清单 (审计发现过程见 handoff 2026-08-02 R4 条目):
  A. 2 笔损坏交易 rpnl 重算: #977 JD / #983 GOOG (EXIT_MONITOR_CLOSE 写错 realized_pnl;
     口径 = R2 终裁 realized_pnl=gross, pnl_net=gross-fees_est)
  B. 3 条指令级陈旧行回填: #117←trade#942, #376←trade#977(修正后), #588←trade#1036
     (交易早已平仓, 指令 exit 字段从未回填; #376 的 pnl=24.18 还是损坏值传播)
  C. 4 笔幽灵收养 trades 回填 decision_id (恢复指令↔交易可追溯)
  D. 41 笔孤儿 trades 打 metadata 标 orphan_no_instruction (decision_id join 不到指令;
     多数=五月 playbook 前时代, 7 月 6 笔=指令未持久化)

显式不改 (调查结论=有意语义/代码侧双注册表, 改数据反造假):
  - executed=1&triggered=0 (20 行): triggered=盘中条件穿越触发; DA 直执行路径不穿触发器
    → 标志语义正确 (direction_axes.py:37 已文档化)
  - exit_reason 小写时代 (sl_hit/time_stop/tp_hit): 小写仍是 risk/ 子系统在产拼写,
    非纯历史; 时代映射留给聚合侧文档
"""
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "ftseek_us.db"

DRY = "--write" not in sys.argv
NOW = datetime.now().isoformat()


def recompute(direction, qty, entry, exit_):
    sign = 1.0 if direction == "LONG" else -1.0
    gross = round(sign * (exit_ - entry) * qty, 2)
    notional = qty * entry
    pct = round(gross / notional, 4) if notional > 0 else 0.0
    return gross, pct


def merge_meta(conn, table, row_id, patch):
    row = conn.execute(f"SELECT metadata_json FROM {table} WHERE id=?", (row_id,)).fetchone()
    meta = {}
    if row and row[0]:
        try:
            meta = json.loads(row[0])
        except Exception:
            meta = {}
    meta.update(patch)
    return json.dumps(meta, ensure_ascii=False, default=str)


def main():
    conn = sqlite3.connect(str(DB))
    actions = []

    # ── A. 损坏交易重算 ──
    for tid in (977, 983):
        t = conn.execute(
            "SELECT symbol, direction, quantity, entry_price, exit_price, realized_pnl, pnl_pct, fees_est "
            "FROM trade_records WHERE id=?", (tid,)).fetchone()
        sym, direction, qty, ep, xp, old_pnl, old_pct, fees = t
        gross, pct = recompute(direction, qty, ep, xp)
        net = round(gross - (fees or 0), 2)
        meta = merge_meta(conn, "trade_records", tid, {
            "r4_correction": {
                "at": NOW, "old_realized_pnl": old_pnl, "old_pnl_pct": old_pct,
                "reason": "EXIT_MONITOR_CLOSE 写入损坏 (R2 对账发现), 按价格数学重算",
            }})
        actions.append(("A", f"trade#{tid} {sym} rpnl {old_pnl}→{gross}, pct {old_pct}→{pct}, net→{net}"))
        if not DRY:
            conn.execute(
                "UPDATE trade_records SET realized_pnl=?, pnl_pct=?, pnl_net=?, metadata_json=? WHERE id=?",
                (gross, pct, net, meta, tid))

    # ── B. 指令级陈旧行回填 (交易已平, exit 字段缺失) ──
    backfill = [  # (instr_id, trade_id)
        (117, 942), (376, 977), (588, 1036),
    ]
    for iid, tid in backfill:
        t = conn.execute(
            "SELECT direction, quantity, entry_price, exit_price, exit_time, exit_reason, realized_pnl, holding_hours "
            "FROM trade_records WHERE id=?", (tid,)).fetchone()
        direction, qty, ep, xp, xtime, xreason, rpnl, hh = t
        # 指令 pnl 口径 = gross (pipeline.py:4503 写的就是 gross realized)
        if tid == 977:  # 用修正后的值 (A 段同事务内已更新或取重算)
            rpnl, _ = recompute(direction, qty, ep, xp)
        _, pct = recompute(direction, qty, ep, xp)
        outcome = "win" if rpnl > 0 else "loss"
        old = conn.execute("SELECT outcome, pnl FROM playbook_instructions WHERE id=?", (iid,)).fetchone()
        actions.append(("B", f"instr#{iid}←trade#{tid} outcome {old[0]}→{outcome}, pnl {old[1]}→{rpnl}, exit={xp}/{xreason}"))
        if not DRY:
            conn.execute(
                "UPDATE playbook_instructions SET outcome=?, exit_price=?, exit_reason=?, pnl=?, pnl_pct=?, "
                "holding_period_hours=? WHERE id=?",
                (outcome, xp, xreason, rpnl, pct, hh, iid))
            # exit_time 列存在则一并回填
            try:
                conn.execute("UPDATE playbook_instructions SET exit_time=? WHERE id=?", (xtime, iid))
            except Exception:
                pass

    # ── C. 幽灵收养 4 笔回填 decision_id ──
    link = {
        1090: "universe_expansion:2026-07-20:EOG",   # ←instr#702
        1092: "DA-20260720180352-US.TTE-5ce6",        # ←instr#684
        1094: "DA-20260720182132-US.UBER-610a",       # ←instr#688
        1095: "DA-20260720180933-US.E-5e3b",          # ←instr#685
    }
    for tid, did in link.items():
        # 安全: 确认 decision_id 在指令表存在且 symbol/date 匹配
        ok = conn.execute("SELECT id FROM playbook_instructions WHERE decision_id=?", (did,)).fetchone()
        if not ok:
            actions.append(("C", f"trade#{tid} SKIP (decision_id {did} 指令表不存在)"))
            continue
        meta = merge_meta(conn, "trade_records", tid, {
            "r4_decision_id_backfill": {"at": NOW, "from_instruction_id": ok[0]}})
        actions.append(("C", f"trade#{tid} decision_id→{did} (instr#{ok[0]})"))
        if not DRY:
            conn.execute("UPDATE trade_records SET decision_id=?, metadata_json=? WHERE id=? AND decision_id=''",
                         (did, meta, tid))

    # ── D. 孤儿 trades 打标 (decision_id 非空但 join 不到指令) ──
    orphans = conn.execute(
        "SELECT t.id, t.entry_time FROM trade_records t "
        "LEFT JOIN playbook_instructions i ON i.decision_id=t.decision_id "
        "WHERE t.decision_id!='' AND i.id IS NULL").fetchall()
    for tid, etime in orphans:
        era = "pre-playbook" if str(etime) < "2026-06-20" else "instruction-not-persisted"
        meta = merge_meta(conn, "trade_records", tid, {
            "orphan_no_instruction": True, "orphan_era": era, "orphan_marked_at": NOW})
        actions.append(("D", f"trade#{tid} orphan ({era})"))
        if not DRY:
            conn.execute("UPDATE trade_records SET metadata_json=? WHERE id=?", (meta, tid))

    for cat, msg in actions:
        print(f"[{cat}] {msg}")
    print(f"\n合计 {len(actions)} 项; 模式={'DRY-RUN' if DRY else 'WRITE'}")
    if not DRY:
        conn.commit()
        print("已提交。")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
