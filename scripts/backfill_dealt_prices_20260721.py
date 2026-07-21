#!/usr/bin/env python3
"""2026-07-21 一次性回填: 今日 PLAYBOOK_SIM 交易用富途 dealt_avg_price 重算.

背景: dual_broker fallback 字段名笔误 (avg_price vs dealt_avg_price) 导致
今日全部 sim 交易用 bid/ask 兜底记账, 价格系统性失真.
范围: trade_records 的 entry_price/exit_price/realized_pnl/pnl_pct;
      playbook_instructions 的 execution_price/exit_price/pnl/pnl_pct.
不动: MFE/MAE/peak/holding_hours 等衍生字段 (影响微小).
前置: DB 已 .backup 至 ftseek_us.db.bak_dealtbackfill_20260721.
"""
import sqlite3
import json
from futu import OpenSecTradeContext, TrdEnv, TrdMarket, RET_OK

DB = "/opt/ftseek/FTSeek-US/data/ftseek_us.db"

# ── 1. 拉富途 sim 全部订单 → {order_id: dealt_avg_price} ──
ctx = OpenSecTradeContext(host="127.0.0.1", port=11111,
                          filter_trdmarket=TrdMarket.US, security_firm="FUTUSECURITIES")
ret, orders = ctx.order_list_query(order_id="", status_filter_list=[], trd_env=TrdEnv.SIMULATE)
ctx.close()
assert ret == RET_OK and orders is not None and not orders.empty, f"order_list_query 失败: ret={ret}"
price_map = {}
for _, o in orders.iterrows():
    p = float(o.get("dealt_avg_price", 0) or 0)
    if p > 0:
        price_map[str(o["order_id"])] = p
print(f"[1] 富途订单映射: {len(price_map)} 笔有价")

# ── 2. 逐笔对比今日 PLAYBOOK 交易 ──
conn = sqlite3.connect(DB, timeout=30)
conn.row_factory = sqlite3.Row
rows = conn.execute("""
    SELECT id, symbol, direction, quantity, entry_price, exit_price,
           futu_order_id, close_futu_order_id, realized_pnl, pnl_pct, metadata_json
    FROM trade_records
    WHERE source_type LIKE 'PLAYBOOK%' AND trade_date = '2026-07-21'
    ORDER BY id
""").fetchall()
print(f"[2] 今日 PLAYBOOK 交易: {len(rows)} 笔")

n_entry_fix = n_exit_fix = n_inst_fix = 0
for r in rows:
    tid, sym, d = r["id"], r["symbol"], r["direction"]
    qty = float(r["quantity"])
    old_entry = float(r["entry_price"] or 0)
    old_exit = float(r["exit_price"] or 0)
    old_pnl = float(r["realized_pnl"] or 0)

    real_entry = price_map.get(str(r["futu_order_id"])) if r["futu_order_id"] else None
    real_exit = price_map.get(str(r["close_futu_order_id"])) if r["close_futu_order_id"] else None

    entry_fix = real_entry is not None and abs(real_entry - old_entry) > 1e-9
    exit_fix = real_exit is not None and abs(real_exit - old_exit) > 1e-9
    if not entry_fix and not exit_fix:
        print(f"  = id={tid} {sym}: 价格一致, 跳过")
        continue

    final_entry = real_entry if entry_fix else old_entry
    final_exit = real_exit if exit_fix else old_exit

    # 重算 pnl (仅已平仓)
    new_pnl = new_pct = None
    if r["close_futu_order_id"] and final_exit > 0:
        if d == "LONG":
            new_pnl = round((final_exit - final_entry) * qty, 2)
            new_pct = round((final_exit - final_entry) / final_entry, 4)
        else:
            new_pnl = round((final_entry - final_exit) * qty, 2)
            new_pct = round((final_entry - final_exit) / final_entry, 4)

    # 更新 trade_records
    if new_pnl is not None:
        conn.execute(
            "UPDATE trade_records SET entry_price=?, exit_price=?, realized_pnl=?, pnl_pct=? WHERE id=?",
            (final_entry, final_exit, new_pnl, new_pct, tid))
    else:
        conn.execute(
            "UPDATE trade_records SET entry_price=? WHERE id=?",
            (final_entry, tid))

    # 同步 playbook_instructions
    inst_msg = ""
    try:
        meta = json.loads(r["metadata_json"] or "{}")
        inst_id = meta.get("playbook_instruction_id")
    except Exception:
        inst_id = None
    if inst_id:
        if new_pnl is not None:
            cur = conn.execute(
                "UPDATE playbook_instructions SET execution_price=?, exit_price=?, pnl=?, pnl_pct=? WHERE id=?",
                (final_entry, final_exit, new_pnl, new_pct, inst_id))
        else:
            cur = conn.execute(
                "UPDATE playbook_instructions SET execution_price=? WHERE id=?",
                (final_entry, inst_id))
        if cur.rowcount > 0:
            n_inst_fix += 1
            inst_msg = f" | inst#{inst_id} 同步"

    n_entry_fix += entry_fix
    n_exit_fix += exit_fix
    pnl_msg = f" | pnl {old_pnl:+.2f} → {new_pnl:+.2f}" if new_pnl is not None else ""
    print(f"  ✎ id={tid} {sym} {d}: entry {old_entry} → {final_entry}"
          + (f" | exit {old_exit} → {final_exit}" if exit_fix else "")
          + pnl_msg + inst_msg)

conn.commit()

# ── 3. 验证 ──
chk = conn.execute(
    "SELECT entry_price, exit_price, realized_pnl, pnl_pct FROM trade_records WHERE id=1097").fetchone()
print(f"[3] 验证 MNSB(id=1097): entry={chk[0]} exit={chk[1]} pnl={chk[2]} pct={chk[3]} (期望 24.25/23.71/-86.4/-0.0223)")
tot = conn.execute(
    "SELECT ROUND(SUM(realized_pnl),2) FROM trade_records WHERE source_type LIKE 'PLAYBOOK%' AND trade_date='2026-07-21' AND exit_price > 0"
).fetchone()[0]
print(f"    今日已平仓总 PnL(回填后): {tot}")
conn.close()
print(f"[完成] entry 修正 {n_entry_fix} 笔 | exit 修正 {n_exit_fix} 笔 | instructions 同步 {n_inst_fix} 笔")
