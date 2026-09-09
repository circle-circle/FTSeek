#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RFC-080 混同数据清理: sim/live 两库互相污染的行各删一份 (保留权威库副本).

用户裁决 2026-09-09: "模拟交易和实盘交易不要混为一谈, 包括历史数据"。

清理对象 (已逐行字段级验证两库有完全一致副本, 删错放的那份不丢信息):
  A. sim 库 trade_records 中 trade_mode='live' 的 3 行 (TTE/PDD/BULL)
     — 真钱记录误进研究账本; 权威副本在 live 库 (#902/903/904) → 删 sim 侧
  B. live 库 trade_records 中 trade_mode='simulate' 的 2 行 (PG/TSM PLAYBOOK_SHADOW)
     — 模拟影子误进实盘账本; 权威副本在 sim 库 (#898/899) → 删 live 侧

不动: sim 库 trade_mode='sim'/'simulate' 的 PLAYBOOK_SIM 行 (本属研究账本, 正确)。
默认 dry-run; --apply 实删 (先自动备份两库到 data/backups/)。
"""
import argparse, shutil, sqlite3, sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SIM = ROOT / "data" / "ftseek_us.db"
LIVE = ROOT / "data" / "ftseek_us_live.db"


def find_contaminants():
    """返回 [(db_label, db_path, row_id, symbol, reason, authoritative_db)]"""
    out = []
    # A. sim 库里的真钱行 (trade_mode='live')
    c = sqlite3.connect(f"file:{SIM}?mode=ro", uri=True); c.row_factory = sqlite3.Row
    for r in c.execute("SELECT id,symbol,trade_mode,source_type FROM trade_records WHERE trade_mode='live'"):
        out.append(("sim", str(SIM), r["id"], r["symbol"],
                    f"真钱记录(trade_mode=live,{r['source_type']})误进研究账本", "live"))
    c.close()
    # B. live 库里的模拟行 (trade_mode != 'live')
    c = sqlite3.connect(f"file:{LIVE}?mode=ro", uri=True); c.row_factory = sqlite3.Row
    for r in c.execute("SELECT id,symbol,trade_mode,source_type FROM trade_records WHERE trade_mode!='live'"):
        out.append(("live", str(LIVE), r["id"], r["symbol"],
                    f"模拟记录(trade_mode={r['trade_mode']},{r['source_type']})误进实盘账本", "sim"))
    c.close()
    return out


def verify_authoritative_copy(db_label, row_id, auth_label):
    """删前确认权威库有同自然键副本 (symbol+entry_time+source_type+quantity)."""
    src = sqlite3.connect(f"file:{SIM if db_label=='sim' else LIVE}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    row = src.execute("SELECT * FROM trade_records WHERE id=?", (row_id,)).fetchone()
    src.close()
    if not row:
        return False, "源行不存在"
    auth_path = LIVE if auth_label == "live" else SIM
    a = sqlite3.connect(f"file:{auth_path}?mode=ro", uri=True)
    dup = a.execute(
        "SELECT id FROM trade_records WHERE symbol=? AND entry_time=? AND source_type=? AND quantity=?",
        (row["symbol"], row["entry_time"], row["source_type"], row["quantity"])).fetchone()
    a.close()
    return (dup is not None), (f"权威库副本 #{dup[0]}" if dup else "⚠ 权威库无副本 — 跳过不删")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="实删 (默认 dry-run)")
    args = ap.parse_args()

    contams = find_contaminants()
    print(f"=== 发现 {len(contams)} 行混同 ===")
    to_delete = []
    for db_label, db_path, row_id, symbol, reason, auth in contams:
        has_copy, msg = verify_authoritative_copy(db_label, row_id, auth)
        flag = "✅可删" if has_copy else "⛔保留"
        print(f"  [{flag}] {db_label}库#{row_id} {symbol:<10} {reason} → {auth}库{msg}")
        if has_copy:
            to_delete.append((db_label, db_path, row_id, symbol))

    skipped = len(contams) - len(to_delete)
    if skipped:
        print(f"\n⚠ {skipped} 行因权威库无副本被保留 (不删, 防丢数据)")

    if not args.apply:
        print(f"\n[dry-run] 将删 {len(to_delete)} 行。加 --apply 实删 (自动备份两库)。")
        return 0

    # 备份
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    bdir = ROOT / "data" / "backups" / f"{ts}-rfc080-demix"
    bdir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SIM, bdir / SIM.name)
    shutil.copy2(LIVE, bdir / LIVE.name)
    print(f"\n[备份] {bdir}")

    # 实删 (按库分组)
    deleted = 0
    for db_label, db_path, row_id, symbol in to_delete:
        c = sqlite3.connect(db_path)
        cur = c.execute("DELETE FROM trade_records WHERE id=?", (row_id,))
        c.commit()
        deleted += cur.rowcount
        print(f"  删除 {db_label}库#{row_id} {symbol} ({cur.rowcount} 行)")
        c.close()

    # 验证: 两库已无混同
    print("\n=== 删后验证 ===")
    c = sqlite3.connect(f"file:{SIM}?mode=ro", uri=True)
    n_sim_live = c.execute("SELECT COUNT(*) FROM trade_records WHERE trade_mode='live'").fetchone()[0]
    c.close()
    c = sqlite3.connect(f"file:{LIVE}?mode=ro", uri=True)
    n_live_sim = c.execute("SELECT COUNT(*) FROM trade_records WHERE trade_mode!='live'").fetchone()[0]
    c.close()
    print(f"  sim 库残留真钱行(trade_mode=live): {n_sim_live} (期望 0)")
    print(f"  live 库残留模拟行(trade_mode!=live): {n_live_sim} (期望 0)")
    ok = (n_sim_live == 0 and n_live_sim == 0)
    print(f"  {'✅ 两库纯净, 无混同' if ok else '⛔ 仍有残留'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
