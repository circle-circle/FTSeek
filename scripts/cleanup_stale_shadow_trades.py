#!/usr/bin/env python3
"""一次性清理脚本: 关闭所有未关闭的 shadow_trades (P0-3 加固 2026-05-27)

背景:
- 系统中积累了 192 笔 closed=0 的 shadow_trades
- 这些交易因为 EOD replay 未覆盖或 mirror_exit 未触发而遗留
- 本脚本一次性清理所有存量, 后续由 position_sync.close_stale_shadow_trades() 每日自动处理

用法:
  python scripts/cleanup_stale_shadow_trades.py                # 执行清理
  python scripts/cleanup_stale_shadow_trades.py --dry-run      # 仅展示, 不写 DB
  python scripts/cleanup_stale_shadow_trades.py --days 30      # 只处理 30 天内的
"""
from __future__ import annotations
import argparse
import logging
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("cleanup_shadow")

DB_PATH = PROJECT_ROOT / "data" / "ftseek_us.db"


def cleanup_stale_shadows(dry_run: bool = False, days: int = 0) -> dict:
    """关闭所有未关闭的 shadow_trades.
    
    Args:
        dry_run: 仅展示不写 DB
        days: 只处理 N 天内的 (0=全部)
    
    Returns:
        统计信息 dict
    """
    if not DB_PATH.exists():
        logger.error(f"DB not found: {DB_PATH}")
        return {"error": "DB not found"}

    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()

    # 查询未关闭的 shadow_trades
    query = "SELECT trade_id, symbol, strategy, direction, entry_price, quantity, entry_time FROM shadow_trades WHERE closed = 0"
    params = []
    if days > 0:
        from datetime import timedelta
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        query += " AND entry_time >= ?"
        params.append(cutoff)
    query += " ORDER BY entry_time"

    cur.execute(query, params)
    rows = cur.fetchall()

    total = len(rows)
    logger.info(f"找到 {total} 笔未关闭的 shadow_trades")

    if total == 0:
        conn.close()
        return {"total": 0, "closed": 0}

    # 按 strategy 分组统计
    by_strategy = {}
    for _, _, strategy, _, _, _, _ in rows:
        by_strategy[strategy] = by_strategy.get(strategy, 0) + 1

    print(f"\n{'='*60}")
    print(f"Shadow Trades 清理统计 (未关闭总计: {total})")
    print(f"{'='*60}")
    for strat, count in sorted(by_strategy.items(), key=lambda x: -x[1]):
        print(f"  {strat:25s}: {count:4d} 笔")
    print(f"{'='*60}\n")

    if dry_run:
        print("[DRY RUN] 不执行实际写入\n")
        # 展示前 20 笔
        print("前 20 笔示例:")
        for trade_id, symbol, strategy, direction, entry_price, quantity, entry_time in rows[:20]:
            print(f"  {trade_id[:16]:16s} | {symbol:12s} | {strategy:18s} | "
                  f"{direction:5s} | entry=${entry_price:.2f} | qty={quantity} | {entry_time}")
        if total > 20:
            print(f"  ... (还有 {total - 20} 笔)")
        conn.close()
        return {"total": total, "closed": 0, "dry_run": True, "by_strategy": by_strategy}

    # 执行批量关闭
    now_str = datetime.now().isoformat()
    closed_count = 0
    error_count = 0

    for trade_id, symbol, strategy, direction, entry_price, quantity, entry_time in rows:
        try:
            # 用 entry_price 作为 exit_price (PnL=0, 表示无法确定真实退出价)
            exit_price = entry_price
            pnl = 0.0
            pnl_pct = 0.0

            # 计算持仓时间
            try:
                entry_dt = datetime.fromisoformat(str(entry_time))
                holding_minutes = (datetime.now() - entry_dt).total_seconds() / 60
            except Exception:
                holding_minutes = 0

            cur.execute("""
                UPDATE shadow_trades SET
                  exit_time = ?, exit_price = ?, realized_pnl = ?,
                  pnl_pct = ?, exit_reason = ?, closed = 1,
                  holding_minutes = ?
                WHERE trade_id = ?
            """, (
                now_str, exit_price, pnl, pnl_pct,
                'batch_cleanup_20260527', holding_minutes, trade_id,
            ))
            closed_count += 1
        except Exception as e:
            logger.warning(f"关闭失败 {trade_id}: {e}")
            error_count += 1

    conn.commit()
    conn.close()

    logger.info(f"✅ 清理完成: closed={closed_count}, errors={error_count}")
    print(f"\n✅ 批量清理完成:")
    print(f"   关闭: {closed_count} 笔")
    print(f"   错误: {error_count} 笔")
    print(f"   退出原因: batch_cleanup_20260527")
    print(f"   退出价格: 使用 entry_price (PnL=0)")

    return {
        "total": total,
        "closed": closed_count,
        "errors": error_count,
        "by_strategy": by_strategy,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="一次性清理未关闭的 shadow_trades")
    parser.add_argument("--dry-run", action="store_true", help="仅展示, 不写 DB")
    parser.add_argument("--days", type=int, default=0, help="只处理 N 天内的 (0=全部)")
    args = parser.parse_args()

    result = cleanup_stale_shadows(dry_run=args.dry_run, days=args.days)
    print(f"\n最终结果: {result}")
