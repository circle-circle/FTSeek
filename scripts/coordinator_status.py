#!/usr/bin/env python3
"""
Coordinator 状态查询 CLI (Phase A 2026-05-14)

随时可跑, 无需服务在线 — 直接读 audit DB 还原最新状态.

用法:
  python scripts/coordinator_status.py
  python scripts/coordinator_status.py --recent-arb 20      # 最近 20 笔仲裁
  python scripts/coordinator_status.py --owner-summary      # 当前持仓的 owner_strategy 分布
  python scripts/coordinator_status.py --capital-from-db    # 从 open_positions 反算账本
"""
from __future__ import annotations
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _open(db_path: str) -> sqlite3.Connection:
    if not os.path.exists(db_path):
        print(f"[ERR] DB 不存在: {db_path}", file=sys.stderr)
        sys.exit(2)
    return sqlite3.connect(db_path)


def cmd_owner_summary():
    from app.core.paths import get_trade_records_db
    conn = _open(str(get_trade_records_db()))
    print("=== 当前 open_positions 的 owner_strategy 分布 ===")
    rows = conn.execute(
        "SELECT COALESCE(owner_strategy, '<NULL>') as o, COUNT(*) as n "
        "FROM open_positions WHERE is_filled=1 AND COALESCE(is_anomalous,0)=0 GROUP BY o"
    ).fetchall()
    if not rows:
        print("  (无活跃持仓)")
    else:
        for o, n in rows:
            print(f"  {o:15s} {n} 笔")
    print()
    print("=== 今日 trade_records 的 owner_strategy 分布 ===")
    rows = conn.execute(
        "SELECT COALESCE(owner_strategy, '<NULL>') as o, COUNT(*) as n "
        "FROM trade_records WHERE date(exit_time) = date('now') GROUP BY o"
    ).fetchall()
    if not rows:
        print("  (今日无成交)")
    else:
        for o, n in rows:
            print(f"  {o:15s} {n} 笔")


def cmd_recent_arbitration(n: int):
    from app.core.paths import get_decisions_db
    conn = _open(str(get_decisions_db()))
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='signal_arbitration'"
    ).fetchone()
    if not cur:
        print("[INFO] signal_arbitration 表尚未创建 (Coordinator 未运行过)")
        return
    print(f"=== 最近 {n} 笔仲裁 ===")
    rows = conn.execute(
        "SELECT datetime(timestamp,'unixepoch','localtime'), symbol, winner_strategy, "
        "winner_score, n_candidates, candidates_json "
        "FROM signal_arbitration ORDER BY id DESC LIMIT ?",
        (n,),
    ).fetchall()
    if not rows:
        print("  (无记录)")
        return
    for ts, sym, win, score, nc, cands_json in rows:
        print(f"  {ts} {sym:6s} winner={win:10s} score={score:.2f} n={nc}")
        if nc > 1:
            try:
                cands = json.loads(cands_json)
                for c in cands:
                    mark = "✓" if c["strategy"] == win else " "
                    print(f"    {mark} {c['strategy']:10s} p={c['priority']} c={c['confidence']:.2f} score={c['score']:.2f} dir={c['direction']}")
            except Exception:
                pass


def cmd_capital_from_db():
    """从 open_positions 反算各策略真实占用 (用于与运行时账本对比)"""
    from app.core.paths import get_trade_records_db
    conn = _open(str(get_trade_records_db()))
    rows = conn.execute(
        "SELECT COALESCE(owner_strategy, '<NULL>') as o, "
        "       SUM(size * avg_entry_price) as total_cost, COUNT(*) as n "
        "FROM open_positions WHERE is_filled=1 AND COALESCE(is_anomalous,0)=0 GROUP BY o"
    ).fetchall()
    print("=== 从 open_positions 反算占用 (size × avg_entry_price 加总) ===")
    if not rows:
        print("  (无活跃持仓)")
    else:
        total = sum(r[1] or 0 for r in rows)
        for o, cost, n in rows:
            cost = cost or 0
            print(f"  {o:15s} {n:3d} 仓  ${cost:12,.2f}  ({cost/total*100 if total else 0:5.1f}%)")
        print(f"  {'TOTAL':15s} {sum(r[2] for r in rows):3d}    ${total:12,.2f}")


def cmd_recent_blocks(n: int = 30):
    """从日志/journal 反查最近的 BLOCKED/CONFLICT 事件 (需要服务在线时的内存 events, 这里说明用法)"""
    print("=== 内存事件 (CapitalAllocator/SymbolLock) ===")
    print("注: events 在内存, 服务重启即失. 看实时事件请查 systemd journal:")
    print("  journalctl -u ftseek-us -n 200 --no-pager | grep -E 'CapitalAllocator|SymbolLock'")


def main():
    ap = argparse.ArgumentParser(description="Coordinator 状态查询")
    ap.add_argument("--recent-arb", type=int, default=10,
                    help="显示最近 N 笔仲裁 (默认 10)")
    ap.add_argument("--owner-summary", action="store_true",
                    help="持仓的 owner_strategy 分布")
    ap.add_argument("--capital-from-db", action="store_true",
                    help="从 open_positions 反算占用")
    ap.add_argument("--all", action="store_true", help="跑全部")
    args = ap.parse_args()

    print(f"[{datetime.now().isoformat(timespec='seconds')}] Coordinator Status")
    print("=" * 60)

    if args.all or args.owner_summary:
        cmd_owner_summary()
        print()
    if args.all or args.capital_from_db:
        cmd_capital_from_db()
        print()
    cmd_recent_arbitration(args.recent_arb)
    print()
    cmd_recent_blocks()


if __name__ == "__main__":
    main()
