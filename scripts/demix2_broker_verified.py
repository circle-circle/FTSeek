#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RFC-080 demix 第二阶段: broker 真值核验后的定向修正.

第一阶段教训: "两库都有副本"不等于"删哪边都对" — 必须用 broker REAL 成交历史
(唯一权威) 判定归属。核验结果: live 库 4 行 external_close (MSFT#900/AVGO#901/
TTE#902/BULL#904) 零 REAL 证据 = sim 仓被 position_sync:1699 硬编码
trade_mode='live' 误标后被 RFC-078 迁移搬进 live 库。

修正 (逐行分类, 全部有 broker 真值/sim 孪生证据):
  TTE  : sim#1036 第一阶段被误删 (方向错) → 从备份恢复回 sim 库 (trade_mode='simulate')
         + 删 live#902
  AVGO : live#901 sim 库无孪生 (孤行) → 移回 sim 库 (trade_mode='simulate') + 删 live#901
  MSFT : live#900 sim 库已有原始记账 #942 (PLAYBOOK_SIM) → external_close 行是重复
         记账 → 删 live#900, 不恢复
  BULL : live#904 sim 库已有原始记账 #1320 → 同上, 删 live#904 (sim#1327 第一阶段已删对)

默认 dry-run; --apply 实改 (自动备份两库)。
"""
import argparse, glob, shutil, sqlite3, sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SIM = ROOT / "data" / "ftseek_us.db"
LIVE = ROOT / "data" / "ftseek_us_live.db"
BK_GLOB = str(ROOT / "data" / "backups" / "*-rfc080-demix" / "ftseek_us.db")

# 列清单 (恢复/移动用, id 不搬由目标库自增)
COLS = ("trade_date,symbol,direction,entry_time,exit_time,entry_price,exit_price,"
        "quantity,realized_pnl,pnl_pct,exit_reason,source_type,mode,trade_mode,"
        "created_at,trace_id,signal_id,futu_order_id,pnl_net")


def _row_dict(conn, table, row_id):
    conn.row_factory = sqlite3.Row
    r = conn.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,)).fetchone()
    return dict(r) if r else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    bks = sorted(glob.glob(BK_GLOB))
    if not bks:
        print("⛔ 找不到第一阶段备份 (demix 备份目录)"); return 1
    bk_sim = bks[-1]
    print(f"恢复源备份: {bk_sim}")

    sim_ro = sqlite3.connect(f"file:{bk_sim}?mode=ro", uri=True)
    live_ro = sqlite3.connect(f"file:{LIVE}?mode=ro", uri=True)

    # ── 计划 ──
    plan = []

    # TTE: 恢复 sim#1036 (备份) → sim 库, trade_mode 改 simulate; 删 live#902
    tte = _row_dict(sim_ro, "trade_records", 1036)
    if not tte:
        print("⛔ 备份中无 sim#1036 (TTE)"); return 1
    # 防重复恢复: sim 库当前已有同自然键行则跳过恢复
    sim_now = sqlite3.connect(f"file:{SIM}?mode=ro", uri=True)
    dup = sim_now.execute(
        "SELECT id FROM trade_records WHERE symbol=? AND entry_time=? AND source_type='POSITION_SYNC'",
        (tte['symbol'], tte['entry_time'])).fetchone()
    sim_now.close()
    plan.append(("restore_sim", tte, None if dup else "trade_mode→simulate",
                 f"TTE sim#1036 恢复回 sim 库 (broker REAL 无成交=sim 仓){' — 已存在#%d 跳过' % dup[0] if dup else ''}"))
    plan.append(("delete_live", 902, None, "TTE live#902 删除 (非真钱, sim 侧已恢复)"))

    # AVGO: live#901 → 移回 sim 库 (trade_mode simulate); 删 live#901
    avgo = _row_dict(live_ro, "trade_records", 901)
    if not avgo:
        print("⛔ live#901 (AVGO) 不存在"); return 1
    _sim_chk = sqlite3.connect(f"file:{SIM}?mode=ro", uri=True)
    dup2 = _sim_chk.execute(
        "SELECT id FROM trade_records WHERE symbol=? AND entry_time=? AND source_type='POSITION_SYNC'",
        (avgo['symbol'], avgo['entry_time'])).fetchone()
    _sim_chk.close()
    plan.append(("restore_sim", avgo, None if dup2 else "trade_mode→simulate",
                 f"AVGO live#901 移回 sim 库 (孤行, broker REAL 无成交){' — 已存在#%d 跳过' % dup2[0] if dup2 else ''}"))
    plan.append(("delete_live", 901, None, "AVGO live#901 删除 (已移回 sim)"))

    # MSFT/BULL: 直接删 live 侧 (sim 库已有原始 PLAYBOOK_SIM 记账, external_close 是重复行)
    for lid, sym, twin in ((900, "MSFT", "sim#942 PLAYBOOK_SIM"), (904, "BULL", "sim#1320 PLAYBOOK_SIM")):
        row = _row_dict(live_ro, "trade_records", lid)
        if row:
            plan.append(("delete_live", lid, None,
                         f"{sym} live#{lid} 删除 (重复记账, sim 侧原始行 {twin} 保留权威)"))

    sim_ro.close(); live_ro.close()

    print(f"\n=== 修正计划 ({len(plan)} 步) ===")
    for op, payload, note, desc in plan:
        print(f"  [{op}] {desc}")

    if not args.apply:
        print("\n[dry-run] 加 --apply 实改 (自动备份两库)")
        return 0

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    bdir = ROOT / "data" / "backups" / f"{ts}-rfc080-demix2"
    bdir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SIM, bdir / SIM.name); shutil.copy2(LIVE, bdir / LIVE.name)
    print(f"\n[备份] {bdir}")

    sim_w = sqlite3.connect(SIM)
    live_w = sqlite3.connect(LIVE)
    for op, payload, note, desc in plan:
        if op == "restore_sim" and note:  # note=None 表示已存在跳过
            row = dict(payload)
            row["trade_mode"] = "simulate"   # 修正误标
            vals = [row.get(c) for c in COLS.split(",")]
            ph = ",".join("?" * len(COLS.split(",")))
            cur = sim_w.execute(f"INSERT INTO trade_records ({COLS}) VALUES ({ph})", vals)
            sim_w.commit()
            print(f"  ✅ 恢复 → sim#{cur.lastrowid} {row['symbol']} (trade_mode=simulate)")
        elif op == "delete_live":
            cur = live_w.execute("DELETE FROM trade_records WHERE id=?", (payload,))
            live_w.commit()
            print(f"  ✅ 删除 live#{payload} ({cur.rowcount} 行)")
    sim_w.close(); live_w.close()

    # ── 终验: 两库纯净 ──
    print("\n=== 终验 ===")
    c = sqlite3.connect(f"file:{LIVE}?mode=ro", uri=True)
    bad_live = c.execute("SELECT COUNT(*) FROM trade_records WHERE trade_mode!='live' OR exit_reason='external_close' AND symbol IN ('US.MSFT','US.AVGO','US.TTE','US.BULL')").fetchone()[0]
    n_live = c.execute("SELECT COUNT(*) FROM trade_records").fetchone()[0]
    c.close()
    c = sqlite3.connect(f"file:{SIM}?mode=ro", uri=True)
    bad_sim = c.execute("SELECT COUNT(*) FROM trade_records WHERE trade_mode='live'").fetchone()[0]
    tte_ok = c.execute("SELECT COUNT(*) FROM trade_records WHERE symbol='US.TTE' AND source_type='POSITION_SYNC'").fetchone()[0]
    avgo_ok = c.execute("SELECT COUNT(*) FROM trade_records WHERE symbol='US.AVGO' AND source_type='POSITION_SYNC' AND trade_mode='simulate'").fetchone()[0]
    c.close()
    print(f"  live 库: {n_live} 行, 残留误标/混同行={bad_live} (期望0)")
    print(f"  sim 库: trade_mode='live' 残留={bad_sim} (期望0), TTE恢复={tte_ok} (期望1), AVGO移回={avgo_ok} (期望1)")
    ok = bad_live == 0 and bad_sim == 0 and tte_ok == 1 and avgo_ok == 1
    print(f"  {'✅ 两库纯净' if ok else '⛔ 仍有残留'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
