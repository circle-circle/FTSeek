#!/usr/bin/env python3
"""
修复当前 open_positions 的 trace_id（从 trade_attribution 回填）

用法: python3 scripts/backfill_open_position_trace_id.py
"""
import sqlite3
from pathlib import Path

DB_PATH = Path("data/ftseek_us.db")

def backfill():
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()

    # 查找所有 trace_id 为空的 open_positions
    cursor.execute("""
        SELECT symbol, direction, entry_time
        FROM open_positions
        WHERE (trace_id IS NULL OR trace_id = '')
          AND (sync_status IS NULL OR sync_status NOT IN
              ('closed_by_daemon', 'closed_by_scheduler', 'closed_by_sync',
               'closed_emergency', 'stale_not_in_broker'))
    """)

    empty_positions = cursor.fetchall()
    if not empty_positions:
        print("✓ 所有 open_positions 都有 trace_id")
        conn.close()
        return

    print(f"找到 {len(empty_positions)} 个缺少 trace_id 的持仓:")
    print("-" * 70)

    fixed = 0
    for symbol, direction, entry_time in empty_positions:
        print(f"\n{symbol} {direction} (entry: {entry_time})")

        # 从 trade_attribution 查找匹配的 trace_id
        cursor.execute("""
            SELECT trace_id, entry_time
            FROM trade_attribution
            WHERE symbol = ?
              AND direction = ?
              AND entry_time <= ?
            ORDER BY entry_time DESC
            LIMIT 1
        """, (symbol, direction, entry_time))

        attr_row = cursor.fetchone()
        if attr_row and attr_row[0]:
            trace_id = attr_row[0]
            print(f"  ✓ 找到 trace_id: {trace_id}")

            # 更新 open_positions
            cursor.execute("""
                UPDATE open_positions
                SET trace_id = ?
                WHERE symbol = ?
                  AND (trace_id IS NULL OR trace_id = '')
            """, (trace_id, symbol))

            fixed += 1
        else:
            print(f"  ✗ 未找到匹配的 trade_attribution 记录")

    conn.commit()
    conn.close()

    print("\n" + "-" * 70)
    print(f"✓ 修复完成: {fixed}/{len(empty_positions)} 个持仓已回填 trace_id")

if __name__ == "__main__":
    backfill()
