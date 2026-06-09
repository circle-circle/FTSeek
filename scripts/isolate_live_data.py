#!/usr/bin/env python3
"""
模拟盘→实盘数据隔离脚本

功能:
1. 备份当前 open_positions 到 open_positions_paper_backup_<date>
2. 清空 open_positions (实盘从空仓开始)
3. 将真实账户的自管持仓写入 stoploss_events (标记为 external, 系统跳过)

用法:
  python3 scripts/isolate_live_data.py          # 预览，不执行
  python3 scripts/isolate_live_data.py --apply  # 实际执行

⚠️ 仅在 TRADE_ENV=1 (模拟盘) 时运行，避免误操作实盘数据
"""

import os
import sys
import sqlite3
from pathlib import Path
from datetime import datetime

DB_PATH = Path(__file__).parent.parent / "data" / "ftseek_us.db"

# 真实账户中自行管理的持仓 (不录入系统，登记为已知幽灵)
SELF_MANAGED_POSITIONS = [
    "US.AMD",
    "US.BABA",
    "HK.00700",
]


def main():
    apply_mode = "--apply" in sys.argv

    print(f"{'='*60}")
    print(f"  模拟盘→实盘 数据隔离脚本")
    print(f"  模式: {'✅ 执行' if apply_mode else '🔍 预览 (加 --apply 执行)'}")
    print(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}")

    # 安全检查: 确认当前是模拟盘
    trade_env = os.getenv("TRADE_ENV", "1")
    if trade_env != "1":
        print(f"\n❌ 安全检查失败: TRADE_ENV={trade_env}，仅允许在模拟盘下运行此脚本")
        sys.exit(1)

    if not DB_PATH.exists():
        print(f"\n❌ 数据库不存在: {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()

    # ── Step 1: 查看当前 open_positions ──
    print(f"\n{'─'*60}")
    print("Step 1: 当前 open_positions")
    print(f"{'─'*60}")

    cur.execute("SELECT symbol, direction, size, avg_entry_price, entry_time FROM open_positions")
    positions = cur.fetchall()
    print(f"  持仓数: {len(positions)}")
    for p in positions:
        print(f"  {p[0]:12s} {p[1]:5s} {p[2]:>8.0f}股 @ ${p[3]:.2f}  {p[4][:10] if p[4] else '?'}")

    if not positions:
        print("  (已是空仓，无需归档)")

    # ── Step 2: 备份 ──
    backup_table = f"open_positions_paper_{datetime.now().strftime('%Y%m%d')}"
    print(f"\n{'─'*60}")
    print(f"Step 2: 备份到 {backup_table}")
    print(f"{'─'*60}")

    if apply_mode and positions:
        cur.execute(f"CREATE TABLE IF NOT EXISTS {backup_table} AS SELECT * FROM open_positions")
        conn.commit()
        print(f"  ✅ 已创建备份表: {backup_table}")
        cur.execute(f"SELECT COUNT(*) FROM {backup_table}")
        print(f"  ✅ 备份行数: {cur.fetchone()[0]}")
    else:
        print(f"  (预览模式，不执行)")

    # ── Step 3: 清空 open_positions ──
    print(f"\n{'─'*60}")
    print("Step 3: 清空 open_positions")
    print(f"{'─'*60}")

    if apply_mode and positions:
        cur.execute("DELETE FROM open_positions")
        conn.commit()
        print(f"  ✅ 已清空 {len(positions)} 条持仓记录")
    else:
        print(f"  (预览模式，不执行)")

    # ── Step 4: 登记自管持仓为已知幽灵 ──
    print(f"\n{'─'*60}")
    print("Step 4: 登记自管持仓为已知幽灵 (系统跳过)")
    print(f"{'─'*60}")

    # 检查 stoploss_events 表结构
    cur.execute("PRAGMA table_info(stoploss_events)")
    cols = [row[1] for row in cur.fetchall()]
    print(f"  stoploss_events 列: {cols}")

    for symbol in SELF_MANAGED_POSITIONS:
        # 检查是否已存在
        cur.execute(
            "SELECT COUNT(*) FROM stoploss_events WHERE symbol = ? AND event_type = 'external_close'",
            (symbol,)
        )
        existing = cur.fetchone()[0]
        if existing > 0:
            print(f"  {symbol}: 已有 {existing} 条 external_close 记录，跳过")
        else:
            print(f"  {symbol}: 需登记")
            if apply_mode:
                cur.execute(
                    "INSERT INTO stoploss_events (symbol, event_type, event_time, details) VALUES (?, ?, ?, ?)",
                    (symbol, "external_close", datetime.now().isoformat(),
                     f"自管持仓，系统不管理，实盘隔离登记于 {datetime.now().strftime('%Y-%m-%d')}")
                )
                conn.commit()
                print(f"    ✅ 已登记 external_close")

    # ── Step 5: 验证 ──
    print(f"\n{'─'*60}")
    print("Step 5: 最终状态")
    print(f"{'─'*60}")

    cur.execute("SELECT COUNT(*) FROM open_positions")
    print(f"  open_positions: {cur.fetchone()[0]} 条")

    cur.execute(
        "SELECT DISTINCT symbol FROM stoploss_events WHERE event_type = 'external_close'"
    )
    ghosts = [row[0] for row in cur.fetchall()]
    print(f"  已知幽灵 (external_close): {ghosts}")

    conn.close()

    print(f"\n{'='*60}")
    if apply_mode:
        print("  ✅ 隔离完成！实盘将从空仓开始，自管持仓被跳过")
    else:
        print("  🔍 预览完毕，确认无误后加 --apply 执行")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
